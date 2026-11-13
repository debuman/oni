use crate::Result;
use std::borrow::Cow;
use std::collections::HashSet;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

pub const MAX_BLOB_BYTES: usize = 32 * 1024 * 1024;
const MAX_DIFF_BYTES: usize = 64 * 1024 * 1024;
const MAX_GIT_OUTPUT_BYTES: u64 = 64 * 1024 * 1024;

fn object_id(value: &[u8]) -> Result<String> {
    if !matches!(value.len(), 40 | 64) || !value.iter().all(u8::is_ascii_hexdigit) {
        return Err("invalid Git object ID".into());
    }
    Ok(String::from_utf8(value.to_vec())?)
}

#[derive(Debug)]
pub struct Commit {
    pub oid: String,
    pub parents: Vec<String>,
    pub author: String,
    pub email: String,
    pub timestamp: i64,
    pub iso_date: String,
    pub display_date: String,
    pub subject: String,
    pub message: String,
}

pub struct Entry {
    pub mode: String,
    pub kind: String,
    pub oid: String,
    pub size: usize,
    pub path: String,
}

pub struct Change {
    pub old_path: String,
    pub path: String,
    pub added: usize,
    pub removed: usize,
    pub binary: bool,
}

pub struct Reference {
    pub name: String,
    pub tag: bool,
    pub oid: String,
    pub timestamp: i64,
    pub author: String,
    pub is_commit: bool,
}

pub struct Repository {
    pub path: PathBuf,
    pub name: String,
    pub description: String,
    pub clone_url: String,
    pub owner: String,
    pub commits: Vec<Commit>,
    pub entries: Vec<Entry>,
    readme_index: Option<usize>,
    license_index: Option<usize>,
    pub has_submodules: bool,
    pub has_pull_requests: bool,
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

pub fn command(path: &Path) -> Command {
    let mut command = Command::new("git");
    command.args([
        "--no-pager",
        "-c",
        "color.ui=false",
        "-c",
        "core.quotePath=false",
        "-C",
    ]);
    command.arg(path);
    command.env("GIT_TERMINAL_PROMPT", "0");
    command.env("TZ", "UTC");
    command
}

impl Repository {
    pub fn open(path: &Path) -> Result<Self> {
        Self::load(path, false)
    }

    // The catalog only needs metadata and the tip commit, not the entire tree
    // and history of every repository.
    pub fn open_index(path: &Path) -> Result<Self> {
        Self::load(path, true)
    }

    fn load(path: &Path, index_only: bool) -> Result<Self> {
        let path = fs::canonicalize(path)?;
        let directory = path
            .file_name()
            .ok_or("repository has no directory name")?
            .to_string_lossy();
        let name = directory
            .strip_suffix(".git")
            .unwrap_or(&directory)
            .to_owned();
        let git_dir = command(&path)
            .args(["rev-parse", "--absolute-git-dir"])
            .output()?;
        if !git_dir.status.success() {
            return Err("input is not a Git repository".into());
        }
        let git_dir = PathBuf::from(String::from_utf8(git_dir.stdout)?.trim_end());
        let metadata = |name: &str| -> String {
            let file = git_dir.join(name);
            // Never read a tracked worktree file or follow a metadata symlink.
            match fs::symlink_metadata(&file) {
                Ok(info) if info.is_file() && info.len() <= 16 * 1024 => fs::read_to_string(file)
                    .unwrap_or_default()
                    .trim()
                    .to_owned(),
                _ => String::new(),
            }
        };
        let mut repo = Self {
            name,
            description: metadata("description"),
            clone_url: metadata("url"),
            owner: metadata("owner"),
            path,
            commits: Vec::new(),
            entries: Vec::new(),
            readme_index: None,
            license_index: None,
            has_submodules: false,
            has_pull_requests: false,
        };
        // Distinguish an empty repository from a missing or invalid input path.
        if command(&repo.path)
            .args(["rev-parse", "--verify", "HEAD"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()?
            .success()
        {
            if index_only {
                repo.commits = repo.read_history(&["--max-count=1", "HEAD"], false)?;
            } else {
                repo.commits = repo.read_history(&["HEAD"], false)?;
                repo.entries = repo.read_entries()?;
                repo.index_navigation();
            }
        }
        Ok(repo)
    }

    pub fn git(&self, args: &[&str]) -> Result<Vec<u8>> {
        let mut child = command(&self.path).args(args).stdout(Stdio::piped()).spawn()?;
        let mut output = Vec::new();
        let result = child.stdout.take().ok_or("Git stdout is unavailable")?
            .take(MAX_GIT_OUTPUT_BYTES + 1).read_to_end(&mut output);
        if result.is_err() || output.len() as u64 > MAX_GIT_OUTPUT_BYTES {
            let _ = child.kill();
            let _ = child.wait();
            result?;
            return Err("Git metadata exceeds the 64 MiB limit".into());
        }
        if !child.wait()?.success() {
            return Err(format!("git {} failed", args.join(" ")).into());
        }
        Ok(output)
    }

    fn read_history(&self, revisions: &[&str], exclude_head: bool) -> Result<Vec<Commit>> {
        if revisions.is_empty() {
            return Ok(Vec::new());
        }
        let mut args = vec![
            "log",
            "-z",
            "--no-show-signature",
            "--format=%H%x00%P%x00%an%x00%ae%x00%at%x00%aI%x00%aD%x00%s%x00%B",
        ];
        args.extend_from_slice(revisions);
        if exclude_head && !self.commits.is_empty() {
            args.extend(["--not", "HEAD"]);
        }
        args.push("--");
        let data = self.git(&args)?;
        let mut fields: Vec<&[u8]> = data.split(|byte| *byte == 0).collect();
        if fields.last() == Some(&&b""[..]) {
            fields.pop();
        }
        if fields.len() % 9 != 0 {
            return Err("unexpected Git commit record".into());
        }
        fields
            .chunks_exact(9)
            .map(|f| {
                Ok(Commit {
                    oid: object_id(f[0])?,
                    parents: f[1].split(|byte| *byte == b' ').filter(|value| !value.is_empty())
                        .map(object_id).collect::<Result<Vec<_>>>()?,
                    author: text(f[2]),
                    email: text(f[3]),
                    timestamp: text(f[4]).parse()?,
                    iso_date: text(f[5]),
                    display_date: text(f[6]),
                    subject: text(f[7]),
                    message: text(f[8]),
                })
            })
            .collect()
    }

    pub fn tagged_history(&self, references: &[Reference]) -> Result<Vec<Commit>> {
        let mut known: HashSet<_> = self
            .commits
            .iter()
            .map(|commit| commit.oid.as_str())
            .collect();
        let revisions: Vec<_> = references
            .iter()
            .filter(|reference| reference.tag && reference.is_commit)
            .map(|reference| reference.oid.as_str())
            .filter(|oid| known.insert(*oid))
            .collect();
        self.read_history(&revisions, true)
    }

    fn read_entries(&self) -> Result<Vec<Entry>> {
        self.git(&["ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"])?
            .split(|byte| *byte == 0)
            .filter(|record| !record.is_empty())
            .map(|record| {
                let tab = record
                    .iter()
                    .position(|byte| *byte == b'\t')
                    .ok_or("invalid Git tree entry")?;
                let header = text(&record[..tab]);
                let fields: Vec<_> = header.split_whitespace().collect();
                if fields.len() != 4 {
                    return Err("invalid Git tree metadata".into());
                }
                let path = String::from_utf8(record[tab + 1..].to_vec())?;
                if Path::new(&path).is_absolute()
                    || path.contains(['\\', ':'])
                    || path.split('/').any(|part| part == ".." || part == ".")
                {
                    return Err("unsafe Git file path".into());
                }
                Ok(Entry {
                    mode: fields[0].into(),
                    kind: fields[1].into(),
                    oid: object_id(fields[2].as_bytes())?,
                    size: fields[3].parse().unwrap_or(0),
                    path,
                })
            })
            .collect()
    }

    pub fn references(&self) -> Result<Vec<Reference>> {
        let data = self.git(&["for-each-ref", "--sort=-creatordate",
            "--format=%(refname)%00%(objectname)%00%(*objectname)%00%(authordate:unix)%00%(*authordate:unix)%00%(authorname)%00%(*authorname)%00%(objecttype)%00%(*objecttype)",
            "refs/heads", "refs/tags"])?;
        data.split(|byte| *byte == b'\n')
            .filter(|record| !record.is_empty())
            .map(|record| {
                let fields: Vec<_> = record.split(|byte| *byte == 0).collect();
                if fields.len() != 9 {
                    return Err("invalid Git reference".into());
                }
                let name = text(fields[0]);
                let annotated = !fields[2].is_empty();
                Ok(Reference {
                    tag: name.starts_with("refs/tags/"),
                    name: name.splitn(3, '/').nth(2).unwrap_or(&name).into(),
                    oid: object_id(fields[if annotated { 2 } else { 1 }])?,
                    timestamp: text(fields[if annotated { 4 } else { 3 }])
                        .parse()
                        .unwrap_or(0),
                    author: text(fields[if annotated { 6 } else { 5 }]),
                    is_commit: fields[if annotated { 8 } else { 7 }] == b"commit",
                })
            })
            .collect()
    }

    pub fn readme(&self) -> Option<&Entry> {
        self.readme_index.map(|index| &self.entries[index])
    }

    pub fn license(&self) -> Option<&Entry> {
        self.license_index.map(|index| &self.entries[index])
    }

    fn index_navigation(&mut self) {
        let find = |names: &[&str]| {
            names.iter().find_map(|name| {
                self.entries
                    .iter()
                    .position(|entry| entry.kind == "blob" && entry.path == *name)
            })
        };
        self.readme_index = find(&[
            "README",
            "README.md",
            "README.txt",
            "readme",
            "readme.txt",
            "README.markdown",
        ]);
        self.license_index = find(&["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"]);
        self.has_submodules = self.entries.iter().any(|entry| entry.path == ".gitmodules");
    }
}

const DIFF_END: &[u8] = b"oni-end-of-diff\n";

pub struct Diff {
    pub changes: Vec<Change>,
    data: Vec<u8>,
    patch_start: usize,
}

impl Diff {
    pub fn patch(&self) -> Cow<'_, str> {
        String::from_utf8_lossy(&self.data[self.patch_start..])
    }
}

fn read_diff(output: &mut impl BufRead) -> Result<Diff> {
    let mut data = Vec::new();
    let mut patch_start = None;
    loop {
        let start = data.len();
        let remaining = MAX_DIFF_BYTES.saturating_sub(data.len()) + 1;
        if (&mut *output)
            .take(remaining as u64)
            .read_until(b'\n', &mut data)?
            == 0
        {
            return Err("Git diff stream ended before its response marker".into());
        }
        if data.len() > MAX_DIFF_BYTES {
            return Err("Git diff exceeds the 64 MiB limit".into());
        }
        // Git echoes non-object stdin lines. In a patch every content line has
        // a diff prefix, so this marker cannot be a source line. Raw numstat
        // filenames may contain newlines and even the marker: ignore it there.
        if &data[start..] == DIFF_END && (start == 0 || patch_start.is_some()) {
            data.truncate(start);
            break;
        }
        if patch_start.is_none() {
            // The combined numstat/patch boundary is a double NUL. Rename
            // records cannot contain it because both paths are nonempty.
            if let Some(offset) = data[start..].windows(2).position(|b| b == b"\0\0") {
                patch_start = Some(start + offset + 2);
            }
        }
    }
    let patch_start = patch_start.unwrap_or(0);
    if !data.is_empty() && patch_start == 0 {
        return Err("missing Git diff separator".into());
    }
    Ok(Diff {
        changes: parse_changes(&data[..patch_start])?,
        data,
        patch_start,
    })
}

pub struct Diffs {
    child: Child,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
}

impl Diffs {
    pub fn new(repo: &Repository) -> Result<Self> {
        let mut child = command(&repo.path)
            .args([
                "diff-tree",
                "--stdin",
                "--root",
                "--no-commit-id",
                "-r",
                "--find-renames=100%",
                "--find-copies=100%",
                "--no-ext-diff",
                "--no-textconv",
                "--numstat",
                "-z",
                "-p",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let input = child.stdin.take().ok_or("Git stdin is unavailable")?;
        let output = BufReader::new(child.stdout.take().ok_or("Git stdout is unavailable")?);
        Ok(Self {
            child,
            input: Some(input),
            output,
        })
    }

    pub fn read(&mut self, commit: &Commit) -> Result<Diff> {
        let input = self.input.as_mut().ok_or("Git diff stream is closed")?;
        write!(input, "{}", commit.oid)?;
        // Explicitly select the first parent, including for merge commits.
        // A root commit has no parents and is handled by --root.
        if let Some(parent) = commit.parents.first() {
            write!(input, " {parent}")?;
        }
        writeln!(input)?;
        input.write_all(DIFF_END)?;
        input.flush()?;
        read_diff(&mut self.output)
    }

    pub fn finish(&mut self) -> Result<()> {
        self.input.take();
        if !self.child.wait()?.success() {
            return Err("Git diff process failed".into());
        }
        Ok(())
    }
}

impl Drop for Diffs {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn parse_changes(data: &[u8]) -> Result<Vec<Change>> {
    let mut records = data.split(|byte| *byte == 0);
    let mut changes = Vec::new();
    while let Some(record) = records.next() {
        if record.is_empty() {
            continue;
        }
        let fields: Vec<_> = record.splitn(3, |byte| *byte == b'\t').collect();
        if fields.len() != 3 {
            return Err("invalid Git diff statistics".into());
        }
        // With -z, renamed/copied files use an empty path followed by two
        // NUL-terminated names. Filenames themselves may contain tabs/newlines.
        let (old_path, path) = if fields[2].is_empty() {
            let old = records
                .next()
                .filter(|name| !name.is_empty())
                .ok_or("missing original diff path")?;
            let new = records
                .next()
                .filter(|name| !name.is_empty())
                .ok_or("missing destination diff path")?;
            (text(old), text(new))
        } else {
            (text(fields[2]), text(fields[2]))
        };
        let binary = fields[0] == b"-" && fields[1] == b"-";
        changes.push(Change {
            old_path,
            path,
            binary,
            added: if binary { 0 } else { text(fields[0]).parse()? },
            removed: if binary { 0 } else { text(fields[1]).parse()? },
        });
    }
    Ok(changes)
}

pub struct Objects {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

impl Objects {
    pub fn new(repo: &Repository) -> Result<Self> {
        let mut child = command(&repo.path)
            .args(["cat-file", "--batch"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let input = child.stdin.take().ok_or("Git stdin is unavailable")?;
        let output = BufReader::new(child.stdout.take().ok_or("Git stdout is unavailable")?);
        Ok(Self {
            child,
            input,
            output,
        })
    }

    pub fn blob(&mut self, oid: &str) -> Result<Vec<u8>> {
        writeln!(self.input, "{oid}")?;
        self.input.flush()?;
        let mut line = String::new();
        self.output.read_line(&mut line)?;
        let fields: Vec<_> = line.split_whitespace().collect();
        if fields.len() != 3 || fields[1] != "blob" {
            return Err("Git object is not a blob".into());
        }
        let size: usize = fields[2].parse()?;
        if size > MAX_BLOB_BYTES {
            return Err("Git blob exceeds the 32 MiB limit".into());
        }
        let mut bytes = vec![0; size];
        self.output.read_exact(&mut bytes)?;
        let mut separator = [0];
        self.output.read_exact(&mut separator)?;
        if separator != [b'\n'] {
            return Err("invalid Git object separator".into());
        }
        Ok(bytes)
    }
}

impl Drop for Objects {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_object_id_record_injection() {
        assert!(object_id(b"../../outside").is_err());
        assert!(object_id(b"--output=/tmp/out").is_err());
        assert!(object_id(&[b'a'; 40]).is_ok());
        assert!(object_id(&[b'0'; 64]).is_ok());
        assert!(object_id(&[b'g'; 40]).is_err());
    }

    #[test]
    fn separates_combined_statistics_from_patch() {
        let data = b"0\t0\t\0old\tname.txt\0new\noni-end-of-diff\nname.txt\0-\t-\timage.png\0\0diff --git a/x b/x\n+oni-end-of-diff\noni-end-of-diff\noni-end-of-diff\n";
        let mut input = &data[..];
        let diff = read_diff(&mut input).unwrap();
        assert_eq!(diff.changes.len(), 2);
        assert_eq!(diff.changes[0].path, "new\noni-end-of-diff\nname.txt");
        assert!(diff.changes[1].binary);
        assert_eq!(diff.patch(), "diff --git a/x b/x\n+oni-end-of-diff\n");
        let empty = read_diff(&mut input).unwrap();
        assert!(empty.changes.is_empty());
        assert!(empty.patch().is_empty());
        assert!(read_diff(&mut &b"1\t0\tx\0"[..]).is_err());
        assert!(read_diff(&mut &b""[..]).is_err());
    }

    #[test]
    fn parses_rename_records_without_splitting_filenames() {
        let changes =
            parse_changes(b"0\t0\t\0old\tname.txt\0new\nname.txt\0-\t-\timage.png\0").unwrap();
        assert_eq!(changes.len(), 2);
        assert_eq!(changes[0].old_path, "old\tname.txt");
        assert_eq!(changes[0].path, "new\nname.txt");
        assert_eq!(changes[0].added + changes[0].removed, 0);
        assert!(changes[1].binary);
        assert!(parse_changes(b"0\t0\t\0old.txt\0").is_err());
    }
}
