mod html;
mod markdown;
mod repo;

use html::{escape, link, url_path, Escaped};
use repo::{Change, Commit, Diffs, Objects, Reference, Repository};
use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

fn save(root: &Path, relative: &str, content: &str) -> Result<()> {
    static NEXT_FILE: AtomicU64 = AtomicU64::new(0);
    if relative.contains(['\\', ':']) || Path::new(relative).components()
        .any(|part| !matches!(part, std::path::Component::Normal(_))) {
        return Err("unsafe output path".into());
    }
    let target = root.join(relative);
    let temporary = target.parent().ok_or("output has no parent")?.join(format!(
        ".oni-write-{}-{}",
        std::process::id(),
        NEXT_FILE.fetch_add(1, Ordering::Relaxed)
    ));
    // Creating a new file and renaming it never follows a preexisting file link.
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(content.as_bytes())?;
    drop(file);
    fs::rename(temporary, target)?;
    Ok(())
}

fn output_directory(root: &Path, relative: &Path) -> Result<()> {
    let mut current = root.to_owned();
    for part in relative.components() {
        if !matches!(part, std::path::Component::Normal(_)) {
            return Err("unsafe output directory".into());
        }
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(info) if info.is_dir() && !info.file_type().is_symlink() => (),
            Ok(_) => return Err("output directory is a link or non-directory".into()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => fs::create_dir(&current)?,
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn prepare_output(repo: &Repository, out: &Path) -> Result<()> {
    fs::create_dir_all(out)?;
    if fs::symlink_metadata(out)?.file_type().is_symlink() {
        return Err("output root must not be a symlink".into());
    }
    output_directory(out, Path::new("commit"))?;
    let mut directories = HashSet::new();
    for entry in repo.entries.iter().filter(|entry| entry.kind == "blob") {
        let path = Path::new("file").join(&entry.path);
        let parent = path.parent().ok_or("output path has no parent")?;
        if directories.insert(parent.to_owned()) {
            output_directory(out, parent)?;
        }
    }
    Ok(())
}

fn file_view(path: &str, bytes: &[u8]) -> (String, String) {
    let mut body = format!(
        "<div class=\"file-caption\"><p>{} <span class=\"summary-text\">({}B)</span></p></div>\n",
        escape(path),
        bytes.len()
    );
    if bytes.contains(&0) {
        body.push_str("<p>Binary file.</p>\n");
        return (body, format!("{}B", bytes.len()));
    }
    body.push_str("<pre id=\"blob\">\n");
    let text = String::from_utf8_lossy(bytes);
    let mut count = 0;
    for (index, line) in text.split_inclusive('\n').enumerate() {
        count = index + 1;
        writeln!(
            body,
            "<a class=\"line\" id=\"l{count}\" href=\"#l{count}\">{count:7}</a> {}",
            Escaped(line.trim_end_matches(['\r', '\n']))
        )
        .unwrap();
    }
    body.push_str("</pre>\n");
    (body, format!("{count}L"))
}

fn files(repo: &Repository, out: &Path, objects: &mut Objects) -> Result<()> {
    let mut table = String::from(
        "<table class=\"listing\" id=\"files\"><thead>\
        <tr><td><b>Mode</b></td><td><b>Name</b></td><td class=\"num\"><b>Size</b></td>\
        </tr></thead><tbody>\n",
    );
    for entry in &repo.entries {
        let mode = match entry.mode.as_str() {
            "100755" => "-rwxr-xr-x",
            "120000" => "lrwxrwxrwx",
            "160000" => "m---------",
            _ => "-rw-r--r--",
        };
        let (name, size) = if entry.kind == "blob" {
            let bytes = objects.blob(&entry.oid)?;
            let (body, size) = file_view(&entry.path, &bytes);
            let prefix = "../".repeat(entry.path.matches('/').count() + 1);
            save(
                out,
                &format!("file/{}.html", entry.path),
                &html::repository(repo, &entry.path, &prefix, &body),
            )?;
            (
                link(&format!("file/{}.html", url_path(&entry.path)), &entry.path),
                size,
            )
        } else {
            (
                format!("{} @ {}", escape(&entry.path), escape(&entry.oid)),
                format!("{}B", entry.size),
            )
        };
        writeln!(
            table,
            "<tr><td>{mode}</td><td>{name}</td><td class=\"num\">{size}</td></tr>"
        )
        .unwrap();
    }
    table.push_str("</tbody></table>\n");
    save(
        out,
        "files.html",
        &html::repository(repo, "files", "", &table),
    )?;
    if let Some(entry) = repo.readme() {
        let bytes = objects.blob(&entry.oid)?;
        let rendered = if entry.path.ends_with(".md") || entry.path.ends_with(".markdown") {
            format!(
                "<article class=\"readme\">\n{}</article>\n",
                markdown::render(&bytes)?
            )
        } else {
            format!(
                "<pre class=\"plain-readme\">{}</pre>\n",
                escape(&String::from_utf8_lossy(&bytes))
            )
        };
        save(
            out,
            "README.html",
            &html::repository(repo, "README", "", &rendered),
        )?;
    }
    Ok(())
}

fn diff_sections(patch: &str) -> Vec<&str> {
    let starts: Vec<_> = patch
        .match_indices("diff --git ")
        .filter(|(offset, _)| *offset == 0 || patch.as_bytes()[offset - 1] == b'\n')
        .map(|(offset, _)| offset)
        .collect();
    starts
        .iter()
        .enumerate()
        .map(|(index, start)| &patch[*start..starts.get(index + 1).copied().unwrap_or(patch.len())])
        .collect()
}

fn diff_html(changes: &[Change], sections: &[&str], available: &HashSet<&str>) -> String {
    let mut out = String::new();
    for (index, section) in sections.iter().enumerate() {
        let Some(change) = changes.get(index) else {
            break;
        };
        let mut lines = section.lines();
        lines.next();
        let file_link = |path: &str| {
            if available.contains(path) {
                link(&format!("../file/{}.html", url_path(path)), path)
            } else {
                escape(path)
            }
        };
        let before = file_link(&change.old_path);
        let after = file_link(&change.path);
        write!(
            out,
            "<pre class=\"diff\"><b id=\"h{index}\">diff --git a/{before} b/{after}</b>\n"
        )
        .unwrap();
        let mut in_hunk = false;
        for (line_index, line) in lines.enumerate() {
            in_hunk |= line.starts_with("@@");
            if !in_hunk {
                if line.starts_with("Binary files ") {
                    out.push_str("Binary files differ.\n");
                }
                continue;
            }
            let class = if line.starts_with("@@") {
                "diff-hunk"
            } else if in_hunk && line.starts_with('+') {
                "diff-add"
            } else if in_hunk && line.starts_with('-') {
                "diff-remove"
            } else {
                ""
            };
            if class.is_empty() {
                writeln!(out, "{}", Escaped(line)).unwrap();
            } else {
                writeln!(out, "<a class=\"{class}\" id=\"h{index}-{line_index}\" href=\"#h{index}-{line_index}\">{}</a>", Escaped(line)).unwrap();
            }
        }
        out.push_str("</pre>\n");
    }
    out
}

fn graph_widths(added: usize, removed: usize) -> (usize, usize) {
    let total = added as u128 + removed as u128;
    if total <= 78 {
        return (added, removed);
    }
    let scaled = |count: usize| {
        if count == 0 {
            0
        } else {
            (count as u128 * 78 / total) as usize + 1
        }
    };
    (scaled(added), scaled(removed))
}

fn commit_page(
    repo: &Repository,
    commit: &Commit,
    changes: &[Change],
    patch: &str,
    available: &HashSet<&str>,
) -> String {
    let mut body = format!(
        "<div class=\"panel commit-summary\"><table><tbody><tr><td>\
        <b>commit</b> {}",
        link(&format!("../commit/{}.html", commit.oid), &commit.oid)
    );
    for parent in &commit.parents {
        write!(
            body,
            "<br><b>parent</b> {}",
            link(&format!("../commit/{parent}.html"), parent)
        )
        .unwrap();
    }
    write!(
        body,
        "<br><b>Author:</b> {} &lt;{}&gt;<br><b>Date:</b> {}\
        <br><br>{}<br><br><b>Diffstat:</b>\n<table><tbody>\n",
        escape(&commit.author),
        link(&format!("mailto:{}", commit.email), &commit.email),
        escape(&commit.display_date),
        escape(commit.message.trim_end()).replace('\n', "<br>\n")
    )
    .unwrap();
    let sections = diff_sections(patch);
    let mut added = 0;
    let mut removed = 0;
    for (index, change) in changes.iter().enumerate() {
        added += change.added;
        removed += change.removed;
        let section = sections.get(index).copied().unwrap_or("");
        let status = if section.contains("\nrename from ") {
            "R"
        } else if section.contains("\ncopy from ") {
            "C"
        } else if section.contains("\nnew file mode ") {
            "A"
        } else if section.contains("\ndeleted file mode ") {
            "D"
        } else {
            "M"
        };
        let graph = if change.binary {
            "binary".into()
        } else {
            let (plus, minus) = graph_widths(change.added, change.removed);
            format!(
                "<span class=\"diff-add\">{}</span><span class=\"diff-remove\">{}</span>",
                "+".repeat(plus),
                "-".repeat(minus)
            )
        };
        let label = if change.old_path == change.path {
            change.path.clone()
        } else {
            format!("{} -> {}", change.old_path, change.path)
        };
        writeln!(
            body,
            "<tr><td class=\"{status}\">{status}</td><td>{}</td>\
            <td> | </td><td class=\"num\">{}</td><td>{graph}</td></tr>",
            link(&format!("#h{index}"), &label),
            change.added + change.removed
        )
        .unwrap();
    }
    write!(
        body,
        "</tbody></table></td></tr></tbody></table></div>\n\
        <div class=\"panel commit-diff\"><table><tbody><tr><td class=\"diff-total\">\
        {} file{} changed, {added} insertion(+), {removed} deletion(-)<br><br></td></tr>\
        <tr><td>{}</td></tr></tbody></table></div>\n",
        changes.len(),
        if changes.len() == 1 { "" } else { "s" },
        diff_html(changes, &sections, available)
    )
    .unwrap();
    html::repository(repo, &commit.subject, "../", &body)
}

fn write_commit(
    repo: &Repository,
    out: &Path,
    commit: &Commit,
    available: &HashSet<&str>,
    diffs: &mut Diffs,
) -> Result<Vec<Change>> {
    let diff = diffs.read(commit)?;
    save(
        out,
        &format!("commit/{}.html", commit.oid),
        &commit_page(repo, commit, &diff.changes, &diff.patch(), available),
    )?;
    Ok(diff.changes)
}

fn history(
    repo: &Repository,
    out: &Path,
    limit: usize,
    available: &HashSet<&str>,
    diffs: &mut Diffs,
) -> Result<()> {
    let mut table = String::from(
        "<table class=\"listing\" id=\"log\"><thead><tr>\
        <td><b>date</b></td><td><b>commit message</b></td>\
        <td class=\"num\"><b>files</b></td><td class=\"num\"><b>+</b></td>\
        <td class=\"num\"><b>-</b></td></tr></thead><tbody>\n",
    );
    for (index, commit) in repo.commits.iter().enumerate() {
        let changes = write_commit(repo, out, commit, available, diffs)?;
        if index < limit {
            let added: usize = changes.iter().map(|change| change.added).sum();
            let removed: usize = changes.iter().map(|change| change.removed).sum();
            writeln!(
                table,
                "<tr><td>{}</td><td>{}</td><td class=\"num\">{}</td>\
                <td class=\"num\">+{added}</td><td class=\"num\">-{removed}</td></tr>",
                html::date(commit.timestamp),
                link(&format!("commit/{}.html", commit.oid), &commit.subject),
                changes.len()
            )
            .unwrap();
        }
    }
    if repo.commits.len() > limit {
        writeln!(table, "<tr><td></td><td colspan=\"4\">{} more commits remaining, fetch the repository</td></tr>",
            repo.commits.len() - limit).unwrap();
    }
    table.push_str("</tbody></table>\n");
    let page = html::repository(repo, "log", "", &table);
    save(out, "log.html", &page)?;
    save(out, "index.html", &page)
}

fn references(repo: &Repository, out: &Path, refs: &[Reference]) -> Result<()> {
    let mut body = String::new();
    for (tag, title, id) in [(false, "Branches", "branches"), (true, "Tags", "tags")] {
        write!(
            body,
            "<h2 class=\"reference-title\">{title}</h2><table class=\"listing\" id=\"{id}\"><thead><tr>\
            <td><b>Name</b></td><td><b>Last commit date</b></td><td><b>Author</b></td>\
            </tr></thead><tbody>\n"
        )
        .unwrap();
        let mut group: Vec<_> = refs
            .iter()
            .filter(|reference| reference.tag == tag)
            .collect();
        group.sort_by(|a, b| {
            b.timestamp
                .cmp(&a.timestamp)
                .then_with(|| a.name.cmp(&b.name))
        });
        for reference in group {
            writeln!(
                body,
                "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
                escape(&reference.name),
                html::date(reference.timestamp),
                escape(&reference.author)
            )
            .unwrap();
        }
        body.push_str("</tbody></table><br>\n");
    }
    save(out, "refs.html", &html::repository(repo, "refs", "", &body))
}

fn feed(
    repo: &Repository,
    base_url: &str,
    entries: &[(&Commit, Option<&str>)],
    name: &str,
) -> String {
    let base = base_url.trim_end_matches('/');
    let mut out = format!(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n\
        <feed xmlns=\"http://www.w3.org/2005/Atom\"><id>{}/{name}</id>\
        <title>{}</title><subtitle>{}</subtitle>\
        <link rel=\"self\" href=\"{}/{name}\"/>",
        escape(base),
        escape(&repo.name),
        escape(&repo.description),
        escape(base)
    );
    let updated = entries
        .iter()
        .max_by_key(|(commit, _)| commit.timestamp)
        .map(|(commit, _)| commit.iso_date.as_str())
        .unwrap_or("1970-01-01T00:00:00Z");
    write!(out, "<updated>{}</updated>", escape(updated)).unwrap();
    for (commit, tag) in entries.iter().take(100) {
        let title = tag
            .map(|tag| format!("[{tag}] {}", commit.subject))
            .unwrap_or_else(|| commit.subject.clone());
        let id = tag
            .map(|tag| format!("urn:git:{}:tag:{}", commit.oid, url_path(tag)))
            .unwrap_or_else(|| format!("urn:git:{}", commit.oid));
        write!(
            out,
            "<entry><id>{}</id><title>{}</title>\
            <published>{}</published><updated>{}</updated>\
            <author><name>{}</name><email>{}</email></author>\
            <link rel=\"alternate\" href=\"{}/commit/{}.html\"/>\
            <content type=\"text\">{}</content></entry>\n",
            escape(&id),
            escape(&title),
            escape(&commit.iso_date),
            escape(&commit.iso_date),
            escape(&commit.author),
            escape(&commit.email),
            escape(base),
            commit.oid,
            escape(&commit.message)
        )
        .unwrap();
    }
    out.push_str("</feed>\n");
    out
}

fn generate(repo: &Repository, out: &Path, limit: usize, base_url: &str) -> Result<()> {
    prepare_output(repo, out)?;
    let mut objects = Objects::new(repo)?;
    files(repo, out, &mut objects)?;
    let refs = repo.references()?;
    references(repo, out, &refs)?;
    let tagged_commits = repo.tagged_history(&refs)?;
    let available: HashSet<_> = repo
        .entries
        .iter()
        .filter(|entry| entry.kind == "blob")
        .map(|entry| entry.path.as_str())
        .collect();
    let mut diffs = Diffs::new(repo)?;
    history(repo, out, limit, &available, &mut diffs)?;
    for commit in &tagged_commits {
        write_commit(repo, out, commit, &available, &mut diffs)?;
    }
    diffs.finish()?;
    let entries: Vec<_> = repo
        .commits
        .iter()
        .take(100)
        .map(|commit| (commit, None))
        .collect();
    save(out, "atom.xml", &feed(repo, base_url, &entries, "atom.xml"))?;
    let by_oid: HashMap<_, _> = repo
        .commits
        .iter()
        .chain(tagged_commits.iter())
        .map(|commit| (commit.oid.as_str(), commit))
        .collect();
    let tags: Vec<_> = refs
        .iter()
        .filter(|reference| reference.tag)
        .filter_map(|reference| {
            by_oid
                .get(reference.oid.as_str())
                .map(|commit| (*commit, Some(reference.name.as_str())))
        })
        .collect();
    save(out, "tags.xml", &feed(repo, base_url, &tags, "tags.xml"))?;
    Ok(())
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("--index") {
        let repositories: Result<Vec<_>> = args[1..]
            .iter()
            .map(|arg| Repository::open_index(Path::new(arg)))
            .collect();
        io::stdout().write_all(html::index(&repositories?).as_bytes())?;
        return Ok(());
    }
    if args.first().map(String::as_str) == Some("--version") {
        println!("sqzer-stagit {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let mut path = None;
    let mut limit = 50;
    let mut base_url = String::from("http://127.0.0.1:4174");
    let mut output = PathBuf::from(".");
    let mut pulls_page = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "-l" | "-u" | "--output" | "--pulls-page" => {
                let value = args.get(index + 1).ok_or("missing option value")?;
                match args[index].as_str() {
                    "-l" => limit = value.parse()?,
                    "-u" => base_url = value.clone(),
                    "--pulls-page" => pulls_page = Some(PathBuf::from(value)),
                    _ => output = value.into(),
                }
                index += 2;
            }
            arg if arg.starts_with('-') => return Err(format!("unknown option {arg}").into()),
            arg => {
                if path.replace(PathBuf::from(arg)).is_some() {
                    return Err("multiple repository paths".into());
                }
                index += 1;
            }
        }
    }
    let path = path.ok_or("usage: sqzer-stagit [-l limit] [-u base-url] [--output directory] repository\n       sqzer-stagit --index repository...")?;
    let mut repo = Repository::open(&path)?;
    repo.has_pull_requests = pulls_page.is_some();
    generate(&repo, &output, limit, &base_url)?;
    if let Some(page) = pulls_page {
        // Only the trusted synchronizer supplies this escaped HTML fragment.
        // Never pass a path from a mirrored repository or webhook payload here.
        let body = std::fs::read_to_string(page)?;
        save(&output, "pulls.html", &html::repository(&repo, "Pull requests", "", &body))?;
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("sqzer-stagit: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn diff_preserves_source_lines_that_resemble_metadata() {
        let changes = [Change {
            old_path: "a.txt".into(),
            path: "a.txt".into(),
            added: 1,
            removed: 1,
            binary: false,
        }];
        let patch = "diff --git a/a.txt b/a.txt\nindex 123..456 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n--- old\n+++ new\n";
        let html = diff_html(&changes, &diff_sections(patch), &HashSet::new());
        assert!(!html.contains("index 123"));
        assert!(!html.contains("--- a/a.txt"));
        assert!(html.contains("--- old</a>"));
        assert!(html.contains("+++ new</a>"));
        assert!(html.contains("class=\"diff-remove\""));
        assert!(html.contains("class=\"diff-add\""));
    }

    #[test]
    fn diff_graph_keeps_proportions_and_small_counts() {
        assert_eq!(graph_widths(17883, 566), (76, 3));
        assert_eq!(graph_widths(1, 1), (1, 1));
        assert_eq!(graph_widths(0, 0), (0, 0));
        assert_eq!(graph_widths(10000, 0), (79, 0));
        assert_eq!(graph_widths(0, 10000), (0, 79));
    }
}
