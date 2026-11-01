"""The attended opening test for phase 4b (SPEC §3.4b, Opening step).

    uv run python tests/live/goodreads_signin.py --email-var GOODREADS_EMAIL_ALICE \
        --password-var GOODREADS_PASSWORD_ALICE --ws ws://localhost:3000/

One **attended** sign-in per profile, run by the owner from the household server, which records
what §3.4b's opening step asks for and prints it as a findings report to paste on
`#24 <https://github.com/ryansull/syncshelf/issues/24>`_:

- whether a challenge appeared;
- the cookie set, and each cookie's expiry;
- the ``POST /review_porter/export/<id>`` response status and body;
- the ``#exportFile`` link-text timestamp format, and what timezone it reads as;
- the export's generation time and the CSV's size.

Two findings decide things that are still open: the link's timezone, which #223 currently reads
as the profile's configured one, and whether sign-in is challenged at all — §3.4b says that if
every sign-in is challenged, 4b ships with sign-in disabled and 4a carries Goodreads. #233 waits
on the same run.

**Attended, and never automated.** §3.4b.7 says sign-in is never automated in tests, so this is
a script a person runs and watches, not part of any suite. It is also deliberately careful:

- it writes **nothing** to the database, and leaves no cookie jar behind unless ``--save-jar``
  asks for one;
- it never prints a cookie value, the CSRF token or the password — only names, domains and
  expiries;
- it **stops** on a WAF challenge or CAPTCHA and reports it, and never retries a rejected
  password, so it cannot lock the account.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from syncshelf.clients.goodreads_export import (
    DOWNLOAD_PATH,
    EXPORT_PATH,
    IMPORT_PATH,
    Challenged,
    ExportError,
    GoodreadsExport,
    ImportPageError,
    SessionExpired,
    parse_import_page,
)
from syncshelf.clients.goodreads_rss import Pacer
from syncshelf.sync.goodreads_session import (
    SidecarBrowser,
    attempt_sign_in,
    load_jar,
    save_jar,
)

ROOT = Path(__file__).parents[2]
BODY_SHOWN = 400
"""Characters of the trigger's response body reported. §3.4b.4 calls it undocumented, so its
shape is itself a finding."""


@dataclass
class Findings:
    """What one attended run observed. Nothing here is a secret."""

    outcome: str = "not reached"
    challenged: bool = False
    cookies: list[str] = field(default_factory=list[str])
    """``name@domain expires=…``, never a value."""
    csrf_present: bool = False
    user_id: str | None = None
    link_text: str | None = None
    """The raw ``#exportFile`` link text, which is a public timestamp, not personal data."""
    link_read_as_utc: str | None = None
    trigger_status: int | None = None
    trigger_body: str | None = None
    generated_at: str | None = None
    csv_bytes: int | None = None
    notes: list[str] = field(default_factory=list[str])


def report(findings: Findings) -> str:
    """The findings as text to paste on #24. Pure, so its shape is unit-tested."""
    lines = [
        "Goodreads opening test findings (SPEC §3.4b, Opening step)",
        f"  sign-in outcome:      {findings.outcome}",
        f"  challenge appeared:   {'yes' if findings.challenged else 'no'}",
        f"  cookies harvested:    {len(findings.cookies)}",
    ]
    lines += [f"    {entry}" for entry in findings.cookies]
    lines += [
        f"  CSRF token found:     {'yes' if findings.csrf_present else 'no'}",
        f"  user id on page:      {findings.user_id or '-'}",
        f"  #exportFile text:     {findings.link_text or '-'}",
        f"  read as (UTC):        {findings.link_read_as_utc or '-'}",
        f"  trigger status:       {findings.trigger_status if findings.trigger_status else '-'}",
        f"  trigger body:         {findings.trigger_body or '-'}",
        f"  export generated at:  {findings.generated_at or '-'}",
        f"  CSV size (bytes):     {findings.csv_bytes if findings.csv_bytes else '-'}",
    ]
    if findings.notes:
        lines.append("  notes:")
        lines += [f"    - {note}" for note in findings.notes]
    return "\n".join(lines)


def cookie_lines(jar: object) -> list[str]:
    """Each cookie as ``name@domain expires=…``. Values never appear."""
    entries: list[str] = []
    for cookie in getattr(jar, "cookies", ()):
        expires = cookie.get("expires")
        when = (
            datetime.fromtimestamp(float(expires), UTC).isoformat()
            if isinstance(expires, (int, float))
            else "session"
        )
        entries.append(f"{cookie['name']}@{cookie['domain']} expires={when}")
    return sorted(entries)


async def run(
    email: str, password: str, ws_endpoint: str, tz: ZoneInfo, jar_file: Path | None
) -> Findings:
    """Sign in, then read the import page and trigger one export, recording what happened."""
    findings = Findings()
    browser = SidecarBrowser(ws_endpoint)
    seed = load_jar(jar_file) if jar_file and jar_file.exists() else None
    result = await attempt_sign_in(browser, seed or load_jar(Path("/nonexistent")), email, password)
    findings.outcome = result.outcome
    findings.challenged = result.outcome == "challenged"
    findings.cookies = cookie_lines(result.jar)
    if result.detail:
        findings.notes.append(result.detail)
    if result.outcome != "ok":
        # A prompt, a challenge or a rejected password: stop here and report. Never retry.
        findings.notes.append("stopped before the export: the sign-in did not complete")
        return findings
    if jar_file is not None:
        save_jar(result.jar, jar_file)
        findings.notes.append(f"jar saved to {jar_file}")

    client = GoodreadsExport(result.jar, pacer=Pacer())
    try:
        await _export_findings(client, findings, tz)
    except Challenged as error:
        findings.challenged = True
        findings.notes.append(f"stopped: {error}")
    except (SessionExpired, ExportError, ImportPageError) as error:
        findings.notes.append(f"stopped: {error}")
    finally:
        await client.aclose()
    return findings


async def _export_findings(client: GoodreadsExport, findings: Findings, tz: ZoneInfo) -> None:
    page = parse_import_page((await client.get(IMPORT_PATH)).text, tz)
    findings.csrf_present = bool(page.csrf_token)
    findings.user_id = page.user_id
    if page.export is not None:
        findings.link_read_as_utc = page.export.generated_at.astimezone(UTC).isoformat()
        findings.generated_at = page.export.generated_at.isoformat()

    triggered = await client.post(
        EXPORT_PATH.format(user_id=page.user_id),
        data={"format": "json"},
        headers={
            "x-csrf-token": page.csrf_token,
            "x-requested-with": "XMLHttpRequest",
            "referer": f"https://www.goodreads.com{IMPORT_PATH}",
            "origin": "https://www.goodreads.com",
        },
    )
    findings.trigger_status = triggered.status_code
    findings.trigger_body = triggered.text[:BODY_SHOWN].strip() or "(empty)"

    downloaded = await client.get(DOWNLOAD_PATH.format(user_id=page.user_id))
    if downloaded.status_code == 404:
        findings.notes.append("the export was not ready immediately; it polls in the service")
        return
    findings.csv_bytes = len(downloaded.content)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--email-var", required=True, help="the .env variable holding the email")
    parser.add_argument(
        "--password-var", required=True, help="the .env variable holding the password"
    )
    parser.add_argument("--ws", required=True, help="the playwright sidecar, ws://host:port/")
    parser.add_argument("--timezone", default="Etc/UTC", help="the profile's IANA timezone")
    parser.add_argument(
        "--save-jar", type=Path, default=None, help="write the harvested jar here (optional)"
    )
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()

    from record import read_env  # noqa: PLC0415 — the live scripts share one .env reader

    env = read_env(args.env)
    email, password = env.get(args.email_var), env.get(args.password_var)
    if not email or not password:
        sys.exit(f"{args.email_var} and {args.password_var} must both be set in {args.env}")
    findings = asyncio.run(run(email, password, args.ws, ZoneInfo(args.timezone), args.save_jar))
    print(report(findings))


if __name__ == "__main__":
    main()
