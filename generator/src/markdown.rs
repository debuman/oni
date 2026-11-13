use crate::Result;
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::ffi::{c_char, c_int, c_uint, c_void};

const MAX_README_BYTES: usize = 4 * 1024 * 1024;
const MAX_RENDERED_BYTES: usize = 32 * 1024 * 1024;

struct RenderBuffer {
    html: Vec<u8>,
    failed: bool,
}

// MD4C's public C API; the parser is a system library, not vendored code.
#[link(name = "md4c-html")]
extern "C" {
    fn md_html(
        input: *const c_char,
        length: c_uint,
        callback: unsafe extern "C" fn(*const c_char, c_uint, *mut c_void),
        context: *mut c_void,
        parser_flags: c_uint,
        renderer_flags: c_uint,
    ) -> c_int;
}

unsafe extern "C" fn append(data: *const c_char, length: c_uint, context: *mut c_void) {
    // md_html invokes this synchronously, with valid data for the duration of the call.
    let output = unsafe { &mut *(context as *mut RenderBuffer) };
    if length == 0 || output.failed {
        return;
    }
    if data.is_null()
        || length as usize > MAX_RENDERED_BYTES.saturating_sub(output.html.len())
        || output.html.try_reserve(length as usize).is_err()
    {
        output.failed = true;
        return;
    }
    let bytes = unsafe { std::slice::from_raw_parts(data.cast::<u8>(), length as usize) };
    output.html.extend_from_slice(bytes);
}

fn sanitize(html: &str) -> String {
    let attributes: HashMap<_, HashSet<_>> = [
        ("a", vec!["href", "title"]),
        ("img", vec!["src", "alt", "title", "width", "height"]),
        ("code", vec!["class"]),
        ("td", vec!["align", "colspan", "rowspan"]),
        ("th", vec!["align", "colspan", "rowspan"]),
        ("ol", vec!["start"]),
        ("input", vec!["checked"]),
        ("details", vec!["open"]),
    ]
    .into_iter()
    .map(|(tag, attrs)| (tag, attrs.into_iter().collect()))
    .collect();
    ammonia::Builder::default()
        .tags(
            [
                "a",
                "abbr",
                "b",
                "blockquote",
                "br",
                "caption",
                "code",
                "dd",
                "del",
                "details",
                "div",
                "dl",
                "dt",
                "em",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "hr",
                "i",
                "img",
                "input",
                "kbd",
                "li",
                "ol",
                "p",
                "pre",
                "s",
                "samp",
                "small",
                "span",
                "strong",
                "sub",
                "summary",
                "sup",
                "table",
                "tbody",
                "td",
                "th",
                "thead",
                "tr",
                "ul",
                "var",
            ]
            .into_iter()
            .collect(),
        )
        .tag_attributes(attributes)
        .generic_attributes(HashSet::new())
        .url_schemes(["http", "https", "mailto"].into_iter().collect())
        .set_tag_attribute_value("input", "type", "checkbox")
        .set_tag_attribute_value("input", "disabled", "")
        .attribute_filter(|tag, name, value| {
            if matches!(name, "href" | "src") && !crate::html::safe_url(value) {
                return None;
            }
            if tag == "code"
                && name == "class"
                && !(value.starts_with("language-")
                    && value.len() <= 80
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte)))
            {
                return None;
            }
            if matches!(name, "width" | "height" | "colspan" | "rowspan")
                && !value
                    .parse::<u32>()
                    .is_ok_and(|number| number > 0 && number <= 8192)
            {
                return None;
            }
            Some(Cow::Borrowed(value))
        })
        .clean(html)
        .to_string()
}

pub fn render(input: &[u8]) -> Result<String> {
    if input.len() > MAX_README_BYTES {
        return Err("Markdown README exceeds the 4 MiB limit".into());
    }
    let length = c_uint::try_from(input.len())?;
    let mut output = RenderBuffer {
        html: Vec::new(),
        failed: false,
    };
    // TABLES, TASKLISTS, PERMISSIVEURLAUTOLINKS, PERMISSIVEEMAILAUTOLINKS.
    let flags = 0x100 | 0x800 | 0x4 | 0x8;
    let result = unsafe {
        md_html(
            input.as_ptr().cast(),
            length,
            append,
            (&mut output as *mut RenderBuffer).cast(),
            flags,
            0,
        )
    };
    if result != 0 {
        return Err(format!("MD4C returned {result}").into());
    }
    if output.failed {
        return Err("Markdown output exceeds its memory limit".into());
    }
    Ok(sanitize(&String::from_utf8(output.html)?))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn renders_tables_tasks_details_and_fenced_code() {
        let source = b"# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n\n<details><summary>More</summary>text</details>\n\n~~~rust\nlet x = 1;\n~~~\n";
        let html = render(source).unwrap();
        for expected in [
            "<h1>Title</h1>",
            "<table>",
            "type=\"checkbox\"",
            "<details>",
            "<code class=\"language-rust\">",
        ] {
            assert!(html.contains(expected), "{expected}: {html}");
        }
    }
}
