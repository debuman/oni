use crate::repo::Repository;
use std::fmt::{self, Write};
use std::time::{SystemTime, UNIX_EPOCH};

pub const SITE: &str = "oni.sqzer.com";
pub const CSP: &str = include_str!("../../security/csp.txt");

// Write escaped spans directly into the destination, without a temporary
// allocation for each source line. ASCII metacharacters are UTF-8 boundaries.
pub struct Escaped<'a>(pub &'a str);

impl fmt::Display for Escaped<'_> {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut start = 0;
        for (index, byte) in self.0.bytes().enumerate() {
            let replacement = match byte {
                b'&' => "&amp;",
                b'<' => "&lt;",
                b'>' => "&gt;",
                b'"' => "&quot;",
                b'\'' => "&#39;",
                _ => continue,
            };
            out.write_str(&self.0[start..index])?;
            out.write_str(replacement)?;
            start = index + 1;
        }
        out.write_str(&self.0[start..])
    }
}

pub fn escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for character in input.chars() {
        out.push_str(match character {
            '&' => "&amp;",
            '<' => "&lt;",
            '>' => "&gt;",
            '"' => "&quot;",
            '\'' => "&#39;",
            _ => {
                out.push(character);
                continue;
            }
        });
    }
    out
}

pub fn url_path(input: &str) -> String {
    let mut out = String::new();
    for byte in input.bytes() {
        if byte.is_ascii_alphanumeric() || b"-._~/".contains(&byte) {
            out.push(byte as char);
        } else {
            write!(out, "%{byte:02X}").unwrap();
        }
    }
    out
}

pub fn link(url: &str, label: &str) -> String {
    if !safe_url(url) {
        return escape(label);
    }
    format!("<a href=\"{}\">{}</a>", escape(url), escape(label))
}

pub fn safe_url(url: &str) -> bool {
    if url
        .bytes()
        .any(|byte| byte <= b' ' || byte == 127 || byte == b'\\')
    {
        return false;
    }
    let first = url.split(['/', '?', '#']).next().unwrap_or("");
    match first.split_once(':') {
        Some((scheme, _)) => ["https", "http", "mailto"]
            .iter()
            .any(|allowed| scheme.eq_ignore_ascii_case(allowed)),
        None => true,
    }
}

// Convert a Unix timestamp to a Gregorian calendar date, in UTC.
pub fn date(timestamp: i64) -> String {
    let days = timestamp.div_euclid(86400) + 719468;
    let era = days.div_euclid(146097);
    let day_of_era = days - era * 146097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = month_index + if month_index < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}")
}

fn document(title: &str, prefix: &str, feed: bool) -> String {
    let mut out = format!(
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n\
         <meta http-equiv=\"Content-Security-Policy\" content=\"{}\">\n\
         <meta name=\"referrer\" content=\"no-referrer\">\n\
         <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n\
         <title>{}</title>\n\
         <link rel=\"icon\" href=\"{prefix}favicon.ico\" sizes=\"16x16 32x32 48x48\">\n\
         <link rel=\"icon\" type=\"image/png\" href=\"{prefix}favicon.png\" sizes=\"64x64\">\n\
         <link rel=\"stylesheet\" href=\"{prefix}style.css\">\n",
        escape(CSP.trim()),
        escape(title)
    );
    if feed {
        write!(out,
            "<link rel=\"alternate\" type=\"application/atom+xml\" title=\"Commits\" href=\"{prefix}atom.xml\">\n\
             <link rel=\"alternate\" type=\"application/atom+xml\" title=\"Tags\" href=\"{prefix}tags.xml\">\n").unwrap();
    }
    out.push_str("</head>\n<body>\n");
    out
}

fn footer(holder: &str) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let year = date(now);
    format!("</section>\n<footer class=\"page-footer\">&copy; {} {}</footer>\n</main>\n</body>\n</html>\n",
        &year[..4], escape(holder))
}

pub fn repository(repo: &Repository, title: &str, prefix: &str, body: &str) -> String {
    let mut out = document(
        &format!("{title} - {} - {}", repo.name, repo.description),
        prefix,
        true,
    );
    write!(
        out,
        "<div class=\"repository-id\">{}.git</div>\n\
        <main class=\"page repository\">\n<header class=\"panel\">\
        <table class=\"repository-summary\"><tbody>\n\
        <tr><td><h1>{}</h1><span class=\"summary-text\"> ~ {}</span></td></tr>\n",
        escape(&repo.name),
        escape(&repo.name),
        escape(&repo.description)
    )
    .unwrap();
    if !repo.clone_url.is_empty() {
        writeln!(
            out,
            "<tr><td><i>git clone {}</i></td></tr>",
            link(&repo.clone_url, &repo.clone_url)
        )
        .unwrap();
    }
    let mut navigation = vec![
        ("log.html".into(), "log"),
        ("files.html".into(), "files"),
        ("refs.html".into(), "refs"),
        ("archive.tar.gz".into(), "archive"),
    ];
    if repo.has_pull_requests {
        navigation.push(("pulls.html".into(), "pull requests"));
    }
    if repo.readme().is_some() {
        navigation.push(("README.html".into(), "README"));
    }
    if let Some(entry) = repo.license() {
        navigation.push((format!("file/{}.html", url_path(&entry.path)), "LICENSE"));
    }
    if repo.has_submodules {
        navigation.push(("file/.gitmodules.html".into(), "submodules"));
    }
    let links = navigation
        .iter()
        .map(|(path, label)| link(&format!("{prefix}{path}"), label))
        .collect::<Vec<_>>()
        .join(" | ");
    write!(
        out,
        "<tr><td><nav aria-label=\"Repository\">{links}</nav></td></tr>\n\
        </tbody></table></header>\n<section class=\"page-content\">\n{body}"
    )
    .unwrap();
    out.push_str(&footer(if repo.owner.is_empty() {
        "debuman"
    } else {
        &repo.owner
    }));
    out
}

pub fn index(repositories: &[Repository]) -> String {
    let mut out = document(SITE, "", false);
    write!(
        out,
        "<main class=\"page catalog\">\n<header class=\"panel\">\
        <table class=\"catalog-title\"><tbody><tr><td><b>{SITE} ~ repositories</b></td></tr>\
        </tbody></table></header>\n<section class=\"page-content\">\n\
        <table class=\"listing\" id=\"index\"><thead><tr><td><b>name</b></td>\
        <td><b>description</b></td><td><b>last commit</b></td></tr></thead><tbody>\n"
    )
    .unwrap();
    let mut sorted: Vec<_> = repositories.iter().collect();
    sorted.sort_by(|left, right| {
        let time = |repo: &Repository| {
            repo.commits
                .first()
                .map(|commit| commit.timestamp)
                .unwrap_or(i64::MIN)
        };
        time(right)
            .cmp(&time(left))
            .then_with(|| left.name.cmp(&right.name))
    });
    for repo in sorted {
        let date = repo
            .commits
            .first()
            .map(|commit| date(commit.timestamp))
            .unwrap_or_default();
        writeln!(
            out,
            "<tr><td>{}</td><td>{}</td><td>{date}</td></tr>",
            link(&format!("{}/log.html", url_path(&repo.name)), &repo.name),
            escape(&repo.description)
        )
        .unwrap();
    }
    out.push_str("</tbody></table>\n");
    out.push_str(&footer(SITE));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn escape_html_and_encode_file_paths() {
        assert_eq!(
            escape("<a x=\"'\">&한글"),
            "&lt;a x=&quot;&#39;&quot;&gt;&amp;한글"
        );
        assert_eq!(url_path("src/a b#?.rs"), "src/a%20b%23%3F.rs");
        assert_eq!(url_path("한"), "%ED%95%9C");
        for input in ["", "plain text", "<a x=\"'\">&한글", "🦀&<é>\"'", "&&"] {
            assert_eq!(Escaped(input).to_string(), escape(input));
        }
    }
    #[test]
    fn utc_calendar_handles_epoch_boundaries_and_leap_days() {
        assert_eq!(date(0), "1970-01-01");
        assert_eq!(date(-1), "1969-12-31");
        assert_eq!(date(951782400), "2000-02-29");
        assert_eq!(date(1709164800), "2024-02-29");
    }
}
