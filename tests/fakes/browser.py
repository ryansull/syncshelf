"""A fake sign-in browser (SPEC §3.4b.2, §3.4b.7).

§3.4b.7 is explicit: sign-in is **never** automated against Goodreads in tests. So the state
machine is driven through this instead of a real browser, and the only thing that exercises the
Playwright side is the attended opening test (#219).

``FakeBrowser`` is told what the page shows, as :class:`PageFacts`, and the real rules in
``classify`` turn that into an outcome — so these tests check SyncShelf's decisions, not a
hand-written outcome.
"""

from dataclasses import dataclass, field

from syncshelf.sync.goodreads_session import (
    Cookie,
    CookieJar,
    PageFacts,
    SignInOutcome,
    classify,
)

GOODREADS_URL = "https://www.goodreads.com/"
PORTAL_URL = "https://www.goodreads.com/ap/signin"


def signed_in() -> PageFacts:
    """The page after a sign-in that worked: back on Goodreads, away from the portal."""
    return PageFacts(url=GOODREADS_URL)


def prompting(**what: bool) -> PageFacts:
    """Still on the portal, showing whichever prompt ``what`` names."""
    return PageFacts(url=PORTAL_URL, **what)


@dataclass
class FakeSignIn:
    """One sign-in attempt against :class:`FakeBrowser`."""

    facts: PageFacts
    after_code: PageFacts | None
    jar_cookies: tuple[Cookie, ...]
    seeded: CookieJar
    closed: bool = False
    submissions: list[str] = field(default_factory=list[str])

    async def submit_credentials(self, email: str, password: str) -> SignInOutcome:
        self.submissions.append("credentials")
        return classify(self.facts)

    async def submit_code(self, code: str) -> SignInOutcome:
        self.submissions.append(f"code:{code}")
        if self.after_code is None:
            raise AssertionError("FakeBrowser was not told what a submitted code leads to")
        self.facts = self.after_code
        return classify(self.facts)

    async def cookies(self) -> list[Cookie]:
        return list(self.jar_cookies)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Opens :class:`FakeSignIn` attempts with a scripted page.

    ``cookies`` is what the context holds when asked, including any non-Goodreads cookies a
    real sign-in through Amazon could leave behind, so the harvest filter is exercised.
    """

    def __init__(
        self,
        facts: PageFacts | None = None,
        *,
        cookies: tuple[Cookie, ...] = (),
        after_code: PageFacts | None = None,
    ) -> None:
        self.facts = facts or signed_in()
        self.cookies = cookies
        self.after_code = after_code
        self.opened: list[FakeSignIn] = []

    async def open_sign_in(self, jar: CookieJar) -> FakeSignIn:
        session = FakeSignIn(self.facts, self.after_code, self.cookies, jar)
        self.opened.append(session)
        return session

    @property
    def last(self) -> FakeSignIn:
        """The most recent attempt, for asserting on it."""
        return self.opened[-1]
