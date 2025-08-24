#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, sys, time, re, html
from datetime import datetime, timezone
from typing import Dict, Generator, List, Optional, Tuple
import requests

def env(name: str, required: bool = True, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if required and not val:
        print(f"Missing environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return val or ""

def build_session(base_url: str, token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "canvas-assignments-script/1.2"
    })
    s.base_url = base_url.rstrip("/")
    return s

def clean_course_name(name: Optional[str]) -> str:
    if not name: return "Untitled"
    n = name
    n = re.sub(r"\s*\([^)]+?\d{4}\)\s*$", "", n)                       # drop "(Spring 2025)"
    n = re.sub(r"\s*\((?:Spring|Fall|Summer|Winter)[^)]+\)\s*$", "", n, flags=re.I)
    n = re.sub(r"-(?:\d{2}|ON\d?)-\d{5,}", "", n, flags=re.I)          # drop "-01-30797"
    n = re.sub(r"\s{2,}", " ", n).strip()
    return n

def parse_iso8601(due_at: Optional[str]) -> Optional[datetime]:
    if not due_at: return None
    try:
        return datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def format_date_only(dt: Optional[datetime]) -> str:
    return dt.date().isoformat() if dt else "No due date"

def next_link_from_headers(headers: Dict[str, str]) -> Optional[str]:
    link_val = headers.get("Link") or headers.get("link") or ""
    for part in link_val.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m: return m.group(1)
    return None

def _get_with_retries(session: requests.Session, url: str, params, max_retries=4, timeout=20) -> requests.Response:
    attempt = 0
    while True:
        resp = session.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "3"))
            time.sleep(retry_after); attempt += 1; continue
        if resp.status_code in (500, 502, 503, 504):
            if attempt >= max_retries: return resp
            time.sleep(2 ** attempt); attempt += 1; continue
        return resp

def paginate(session: requests.Session, url: str, params) -> Generator[Dict, None, None]:
    while url:
        resp = _get_with_retries(session, url, params)
        if not resp.ok: resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            for item in data: yield item
        else:
            yield data
        url = next_link_from_headers(resp.headers)
        params = None

def list_courses_generic(session: requests.Session, endpoint: str, term_sub: str, max_courses: int) -> List[Dict]:
    url = f"{session.base_url}{endpoint}"
    params = [
        ("enrollment_type[]", "student"),
        ("enrollment_state[]", "active"),
        ("enrollment_state[]", "completed"),
        ("include[]", "term"),
        ("per_page", "100"),
    ]
    matches: List[Dict] = []
    for c in paginate(session, url, params):
        term = (c.get("term") or {}).get("name", "")
        if term_sub.lower() in term.lower():
            matches.append(c)
        if len(matches) >= max_courses: break
    return matches

def list_my_courses_for_term(session: requests.Session, term_name_substring: str, max_courses: int = 2, source: str = "courses") -> List[Dict]:
    endpoints = ["/api/v1/courses", "/api/v1/users/self/courses"] if source == "courses" else ["/api/v1/users/self/courses", "/api/v1/courses"]
    for ep in endpoints:
        try:
            res = list_courses_generic(session, ep, term_name_substring, max_courses)
            if res: return res
        except requests.HTTPError as e:
            if not (e.response is not None and e.response.status_code >= 500): raise
    return []

def list_assignments(session: requests.Session, course_id: int) -> List[Dict]:
    url = f"{session.base_url}/api/v1/courses/{course_id}/assignments"
    items: List[Dict] = []
    for a in paginate(session, url, [("per_page", "100")]):
        if "published" in a and not a.get("published", False): continue
        items.append(a)
    return items

def sort_assignments(assignments: List[Dict]) -> List[Dict]:
    def key(a: Dict) -> Tuple[int, datetime]:
        due = parse_iso8601(a.get("due_at"))
        return (1, datetime.max.replace(tzinfo=timezone.utc)) if due is None else (0, due)
    return sorted(assignments, key=key)

# ---------- renderers ----------
def render_text(title: str, courses: List[Dict], assignments_by_course: Dict[int, List[Dict]]) -> str:
    lines = [title, ""]
    for course in courses:
        cid = course["id"]
        cname = clean_course_name(course.get("name"))
        lines.append(f"Course: {cname} (ID: {cid})")
        for a in assignments_by_course.get(cid, []):
            due_str = format_date_only(parse_iso8601(a.get("due_at")))
            aname = (a.get("name") or "Untitled").strip()
            lines.append(f'- "{aname}" | Due: {due_str}')
        lines.append("")
    return "\n".join(lines)

def render_md(title: str, courses: List[Dict], assignments_by_course: Dict[int, List[Dict]]) -> str:
    lines = [f"# {title}", ""]
    for course in courses:
        cid = course["id"]
        cname = clean_course_name(course.get("name"))
        lines.append(f"## Course: {cname} (ID: {cid})")
        lines.append("| Assignment | Due |")
        lines.append("|---|---|")
        for a in assignments_by_course.get(cid, []):
            due_str = format_date_only(parse_iso8601(a.get("due_at")))
            aname = (a.get("name") or "Untitled").strip().replace("|", r"\|")
            lines.append(f"| {aname} | {due_str} |")
        lines.append("")
    return "\n".join(lines)

def render_csv(title: str, courses: List[Dict], assignments_by_course: Dict[int, List[Dict]]) -> str:
    rows = ["course_id,course_name,assignment,due_date"]
    for course in courses:
        cid = course["id"]
        cname = clean_course_name(course.get("name")).replace(",", " ")
        for a in assignments_by_course.get(cid, []):
            due_str = format_date_only(parse_iso8601(a.get("due_at")))
            aname = (a.get("name") or "Untitled").strip().replace(",", " ")
            rows.append(f"{cid},{cname},{aname},{due_str}")
    return "\n".join(rows)

def render_html(title: str, courses: List[Dict], assignments_by_course: Dict[int, List[Dict]]) -> str:
    parts = [f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:24px; color:#111}}
h1{{font-size:24px; margin:0 0 16px}}
h2{{font-size:18px; margin:24px 0 8px}}
table{{border-collapse:collapse; width:100%; margin-bottom:12px}}
th,td{{border:1px solid #ddd; padding:8px; vertical-align:top}}
th{{background:#f7f7f7; text-align:left}}
.muted{{color:#666; font-style:italic}}
.container{{max-width:960px; margin:0 auto}}
</style></head><body><div class="container">
<h1>{html.escape(title)}</h1>
"""]
    for course in courses:
        cid = course["id"]
        cname = clean_course_name(course.get("name"))
        parts.append(f"<h2>Course: {html.escape(cname)} (ID: {cid})</h2>")
        parts.append("<table><thead><tr><th>Assignment</th><th>Due</th></tr></thead><tbody>")
        for a in assignments_by_course.get(cid, []):
            due_str = format_date_only(parse_iso8601(a.get("due_at")))
            aname = (a.get("name") or "Untitled").strip()
            parts.append(f"<tr><td>{html.escape(aname)}</td><td>{html.escape(due_str)}</td></tr>")
        parts.append("</tbody></table>")
    parts.append("</div></body></html>")
    return "".join(parts)

def build_output(fmt: str, title: str, courses: List[Dict], assignments_by_course: Dict[int, List[Dict]]) -> str:
    if fmt == "md":   return render_md(title, courses, assignments_by_course)
    if fmt == "html": return render_html(title, courses, assignments_by_course)
    if fmt == "csv":  return render_csv(title, courses, assignments_by_course)
    return render_text(title, courses, assignments_by_course)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Canvas API: show assignments for two courses grouped by course.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--courses", nargs="+", type=int, help="Two course IDs. Example: --courses 12345 67890")
    grp.add_argument("--term", type=str, help='Term name substring, e.g. "Spring 2025"')
    p.add_argument("--max", type=int, default=2, help="Max number of courses (default: 2)")
    p.add_argument("--source", choices=["courses", "self"], default="courses", help="Endpoint to list courses")
    p.add_argument("--title", type=str, default="Courses & Assignments (sorted by due date)")
    p.add_argument("--format", choices=["text","md","html","csv"], default="text", help="Output format")
    p.add_argument("--out", type=str, help="Write output to file")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    base_url = env("CANVAS_BASE_URL")
    token = env("CANVAS_TOKEN")
    s = build_session(base_url, token)

    if args.courses:
        courses = []
        for cid in args.courses[: args.max]:
            r = _get_with_retries(s, f"{s.base_url}/api/v1/courses/{cid}", [("include[]","term")])
            if not r.ok:
                print(f"Failed to fetch course {cid}: {r.status_code}", file=sys.stderr); continue
            courses.append(r.json())
        if not courses:
            print("No courses fetched. Check IDs or permissions.", file=sys.stderr); sys.exit(1)
    else:
        courses = list_my_courses_for_term(s, args.term, args.max, source=args.source)
        if not courses:
            print(f"No courses found for term containing: {args.term!r}.", file=sys.stderr); sys.exit(1)

    assignments_by_course = {}
    for c in courses:
        cid = c["id"]
        try:
            all_as = list_assignments(s, cid)
        except requests.HTTPError as e:
            print(f"Failed to fetch assignments for {cid}: {e}", file=sys.stderr); all_as = []
        trimmed = [{
            "name": (a.get("name") or "Untitled").strip(),
            "due_at": a.get("due_at"),
            "published": a.get("published", True)
        } for a in all_as]
        assignments_by_course[cid] = sort_assignments(trimmed)

    title = args.title if not args.term else f"{args.term} Courses & Assignments (sorted by due date)"
    out_str = build_output(args.format, title, courses, assignments_by_course)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_str)
        print(f"Wrote {args.out}")
    else:
        print(out_str)

if __name__ == "__main__":
    main()
