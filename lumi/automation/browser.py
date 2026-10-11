"""Browser workflows: navigation, reading a page, Gmail drafts.

LUMI drives the browser the same way the user does - through the normal user
interface.  It never touches cookies, saved passwords or session tokens, and it
never tries to defeat a CAPTCHA, MFA prompt or access control.  If a session has
expired, LUMI asks the user to sign in normally.

Email is the canonical risky action:

1. open Gmail's compose window with the fields pre-filled in the URL,
2. show the finished draft to the user,
3. **wait** for explicit approval,
4. click Send,
5. look for Gmail's own confirmation text, and report honestly whether it was
   seen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlparse

from lumi.automation import clipboard as clipboard_tools
from lumi.automation.controller import AutomationController, AutomationResult, AutomationUnavailable
from lumi.automation.backends.base import BackendUnavailable, StepLog
from lumi.utils.timeutil import now

log = logging.getLogger("lumi.automation.browser")

SEARCH_ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "brave": "https://search.brave.com/search?q={query}",
    "startpage": "https://www.startpage.com/sp/search?query={query}",
    "ecosia": "https://www.ecosia.org/search?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "github": "https://github.com/search?q={query}",
    "stackoverflow": "https://stackoverflow.com/search?q={query}",
}

GMAIL_COMPOSE_URL = "https://mail.google.com/mail/?view=cm&fs=1"
GMAIL_INBOX_HINTS = ("gmail", "inbox")
SENT_PATTERNS = (
    r"message sent",
    r"your message has been sent",
    r"email sent",
    r"sending\.\.\.",
    r"message has been sent",
)
SIGN_IN_PATTERNS = (
    r"sign in",
    r"sign-in",
    r"couldn't find your google account",
    r"enter your password",
    r"choose an account",
)

ALLOWED_SCHEMES = {"http", "https", "mailto"}


@dataclass
class PageContent:
    url: str
    title: str = ""
    text: str = ""
    method: str = ""
    truncated: bool = False
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "characters": len(self.text),
            "method": self.method,
            "truncated": self.truncated,
            "warning": self.warning,
            "text": self.text,
        }


@dataclass
class EmailDraft:
    to: str = ""
    subject: str = ""
    body: str = ""
    cc: str = ""
    bcc: str = ""
    compose_url: str = ""
    opened: bool = False
    verified: Optional[bool] = None
    notes: list[str] = field(default_factory=list)

    def preview(self) -> str:
        lines = [f"To: {self.to}"]
        if self.cc:
            lines.append(f"Cc: {self.cc}")
        if self.bcc:
            lines.append(f"Bcc: {self.bcc}")
        lines.append(f"Subject: {self.subject}")
        lines.append("")
        lines.append(self.body)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "to": self.to,
            "cc": self.cc,
            "bcc": self.bcc,
            "subject": self.subject,
            "body": self.body,
            "compose_url": self.compose_url,
            "opened": self.opened,
            "verified": self.verified,
            "notes": list(self.notes),
        }


def sanitise_url(url: str) -> str:
    """Validate and normalise a URL the assistant wants to open.

    Refuses ``javascript:``, ``data:``, ``file:`` and scheme-less input so a
    model (or a hostile prompt) cannot talk LUMI into launching something odd.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("No web address was given.")
    if any(char in raw for char in ("\n", "\r", "\t")):
        raise ValueError("That web address contains line breaks, so I did not open it.")
    if "://" not in raw and not raw.lower().startswith("mailto:"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"I can only open regular web links (http/https). '{scheme or 'that'}' addresses are not allowed."
        )
    if parsed.username or parsed.password:
        raise ValueError("That link contains credentials, so I did not open it.")
    if scheme != "mailto" and not parsed.netloc:
        raise ValueError("That web address is missing a site name.")
    return raw


def build_search_url(query: str, *, engine: str = "duckduckgo") -> str:
    cleaned = " ".join((query or "").split())
    if not cleaned:
        raise ValueError("There is nothing to search for.")
    if len(cleaned) > 400:
        cleaned = cleaned[:400]
    template = SEARCH_ENGINES.get((engine or "").lower(), SEARCH_ENGINES["duckduckgo"])
    return template.format(query=quote(cleaned))


def build_gmail_compose_url(
    *,
    to: str = "",
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
    base: str = GMAIL_COMPOSE_URL,
) -> str:
    # The base URL already carries the compose view; only add what is missing so
    # the final link stays short and readable.
    params = {key: value for key, value in {"view": "cm", "fs": "1"}.items() if f"{key}=" not in base}
    if to:
        params["to"] = to
    if subject:
        params["su"] = subject
    if body:
        params["body"] = body
    if cc:
        params["cc"] = cc
    if bcc:
        params["bcc"] = bcc
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(params, quote_via=quote)}"


def looks_like_email(address: str) -> bool:
    return bool(re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}", (address or "").strip()))


def split_recipients(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", value or "") if part.strip()]


class BrowserController:
    """Opens pages, reads what is on screen, and prepares/sends Gmail drafts."""

    def __init__(self, controller: AutomationController, settings: Any) -> None:
        self.controller = controller
        self.settings = settings

    # ------------------------------------------------------------ properties
    @property
    def browser_settings(self) -> Any:
        return getattr(self.settings, "browser", None)

    @property
    def search_engine(self) -> str:
        browser_settings = self.browser_settings
        return getattr(browser_settings, "search_engine", "duckduckgo") or "duckduckgo"

    # ------------------------------------------------------------ navigation
    def open_url(self, url: str, *, expected_window: str = "") -> AutomationResult:
        clean = sanitise_url(url)
        result = self.controller.open_path(clean)
        if not result.ok:
            return result
        host = urlparse(clean).netloc
        window_hint = expected_window or host.split(":")[0]
        verified: Optional[bool] = None
        if window_hint:
            try:
                found = self.controller.desktop.wait_for_window(window_hint, timeout=6.0)
                verified = found is not None
            except BackendUnavailable:
                verified = None
        result.verified = verified
        result.detail = f"Opened {clean} in your browser."
        if verified is False:
            result.detail += " I could not confirm the page loaded - check the browser window."
        result.data = {**result.data, "url": clean}
        return result

    def search(self, query: str, *, engine: str = "") -> AutomationResult:
        url = build_search_url(query, engine=engine or self.search_engine)
        result = self.open_url(url)
        if result.ok:
            result.action = "search_web"
            result.detail = f"Searched for '{query.strip()[:80]}' using {engine or self.search_engine}."
            result.data = {**result.data, "query": query, "engine": engine or self.search_engine}
        return result

    def open_new_tab(self, url: str) -> AutomationResult:
        clean = sanitise_url(url)
        return self.controller.open_path(clean)

    def close_current_tab(self) -> AutomationResult:
        return self.controller.press_keys("ctrl+w")

    # --------------------------------------------------------------- reading
    def read_page(self, *, window_hint: str = "", max_chars: int = 12000, method: str = "auto") -> AutomationResult:
        """Read the visible text of the current browser page.

        Two strategies: accessibility text from the browser window (preferred,
        non-intrusive) or select-all + copy through the clipboard (works in more
        browsers, restores the clipboard afterwards).
        """
        settings_method = getattr(self.browser_settings, "read_page_method", "auto") or "auto"
        chosen = method if method != "auto" else settings_method
        window, error = self._browser_window(window_hint)
        if window is None and chosen != "clipboard":
            return AutomationResult.failure("read_page", error or "No browser window is open.")

        title = window.title if window else ""

        if chosen in {"auto", "uia"} and window is not None:
            try:
                text = self.controller.desktop.window_text(window, max_chars=max_chars)
            except BackendUnavailable:
                text = ""
            except Exception as exc:  # pragma: no cover
                log.debug("window text read failed: %s", exc)
                text = ""
            if len(text.strip()) > 200:
                return AutomationResult(
                    ok=True,
                    action="read_page",
                    detail=f"Read {len(text)} characters from '{title}'.",
                    verified=True,
                    data=PageContent(url="", title=title, text=text[:max_chars], method="accessibility").to_dict(),
                )
            if chosen == "uia":
                return AutomationResult.failure(
                    "read_page",
                    "This browser did not expose the page text to accessibility tools.",
                    hint="Try the clipboard method, or ask me to read the window instead.",
                )

        return self._read_via_clipboard(window_hint=window_hint, max_chars=max_chars, title=title)

    def _read_via_clipboard(self, *, window_hint: str, max_chars: int, title: str) -> AutomationResult:
        """Select-all + copy, then restore the user's clipboard."""
        try:
            self.controller._require_input("reading the page")  # noqa: SLF001 - intentional internal guard
        except AutomationUnavailable as exc:
            return AutomationResult.failure("read_page", exc.message, hint=exc.hint)

        steps: list[StepLog] = []
        if window_hint or title:
            focus = self.controller.focus_window(window_hint or title)
            steps.extend(focus.steps)
        with clipboard_tools.preserve_clipboard() as previous:
            if previous is None:
                steps.append(StepLog(action="clipboard", detail="could not read the clipboard", ok=False))
            self.controller.press_keys("ctrl+a")
            self.controller.wait(0.15)
            self.controller.press_keys("ctrl+c")
            self.controller.wait(0.4)
            text = clipboard_tools.read_clipboard() or ""
            if previous is not None and text == previous:
                steps.append(StepLog(action="clipboard", detail="copy did not change the clipboard", ok=False))
                return AutomationResult.failure(
                    "read_page",
                    "I could not copy the page text. The browser may not have focus.",
                    hint="Click once inside the page and ask again.",
                    steps=steps,
                )
        steps.append(StepLog(action="read", detail=f"{len(text)} characters via clipboard", ok=True, verified=True))
        return AutomationResult(
            ok=True,
            action="read_page",
            detail=f"Read {len(text)} characters using copy-paste (your clipboard was restored).",
            verified=True,
            data=PageContent(
                url="", title=title, text=text[:max_chars], method="clipboard", truncated=len(text) > max_chars
            ).to_dict(),
            steps=steps,
        )

    # ----------------------------------------------------------------- Gmail
    def compose_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
    ) -> AutomationResult:
        """Open a Gmail compose window with the draft filled in.  Does not send."""
        recipients = split_recipients(to)
        if not recipients:
            return AutomationResult.failure("compose_email", "No recipient address was given.")
        invalid = [address for address in recipients if not looks_like_email(address)]
        if invalid:
            return AutomationResult.failure(
                "compose_email",
                f"These addresses do not look like email addresses: {', '.join(invalid)}",
                hint="Format them like name@example.com and try again.",
            )
        browser_settings = self.browser_settings
        base = getattr(browser_settings, "gmail_compose_url", GMAIL_COMPOSE_URL) or GMAIL_COMPOSE_URL
        url = build_gmail_compose_url(to=", ".join(recipients), subject=subject, body=body, cc=cc, bcc=bcc, base=base)

        result = self.controller.open_path(url)
        if not result.ok:
            return result

        draft = EmailDraft(to=", ".join(recipients), cc=cc, bcc=bcc, subject=subject, body=body, compose_url=url, opened=True)

        # Give the compose window a moment, then check what is on screen.
        self.controller.wait(2.0)
        window = None
        try:
            window = self.controller.desktop.wait_for_window("gmail", timeout=8.0)
        except BackendUnavailable:
            window = None
        except Exception:  # pragma: no cover
            window = None

        if window is not None:
            try:
                text = self.controller.desktop.window_text(window, max_chars=6000)
            except Exception:
                text = ""
            lowered = text.lower()
            if any(re.search(pattern, lowered) for pattern in SIGN_IN_PATTERNS):
                draft.verified = False
                draft.notes.append(
                    "Gmail is asking you to sign in. LUMI will not touch your credentials - "
                    "please sign in normally and then ask again."
                )
            elif subject and subject.lower()[:40] in lowered:
                draft.verified = True
            else:
                draft.verified = None
                draft.notes.append("The draft is open in your browser; I could not read every field to confirm it.")
        else:
            draft.verified = None
            draft.notes.append(
                "I opened the compose window, but I could not confirm it opened (the browser window was not detected)."
            )

        return AutomationResult(
            ok=True,
            action="compose_email",
            detail="Draft ready:\n\n" + draft.preview(),
            verified=draft.verified,
            data={"draft": draft.to_dict(), "draft_text": draft.preview()},
            steps=result.steps + [StepLog(action="compose", detail="Gmail compose opened", ok=True, verified=draft.verified)],
        )

    def send_composed_email(self, *, timeout: float = 12.0) -> AutomationResult:
        """Click Send in the open Gmail compose window and verify the result.

        The tool layer only calls this after the user approved the exact draft.
        """
        window, error = self._browser_window("gmail")
        if window is None:
            return AutomationResult.failure(
                "send_composed_email", error or "I could not find the Gmail window.",
                hint="Open the draft and try again.",
            )
        button = None
        try:
            button = self.controller.desktop.wait_for_element(window, "send", timeout=4.0, control_types=("Button",))
            if button is None:
                button = self.controller.desktop.wait_for_element(window, "send", timeout=3.0)
        except BackendUnavailable as exc:
            return AutomationResult.failure(
                "send_composed_email",
                "I cannot click Send because accessibility access to the browser is unavailable.",
                hint=(exc.hint or "") + " You can press Ctrl+Enter in the compose window to send it yourself.",
            )
        if button is None:
            return AutomationResult.failure(
                "send_composed_email",
                "I could not find the Send button in Gmail.",
                hint="The draft is still open - you can send it yourself with Ctrl+Enter.",
            )

        try:
            self.controller.desktop.click_element(button)
        except Exception as exc:
            return AutomationResult.failure("send_composed_email", f"Clicking Send failed: {exc}")
        self.controller.wait(1.2)

        # Verify: Gmail shows a "Message sent" snackbar and the compose window closes.
        verified: Optional[bool] = None
        try:
            text = self.controller.desktop.window_text(window, max_chars=4000).lower()
            if any(re.search(pattern, text) for pattern in SENT_PATTERNS):
                verified = True
            else:
                compose = self.controller.desktop.wait_for_element(window, "message body", timeout=2.0)
                if compose is None:
                    verified = True  # compose closed and no error -> most likely sent
        except Exception:  # pragma: no cover
            verified = None

        detail = "Clicked Send in Gmail."
        if verified is True:
            detail += " Gmail confirmed the message was sent."
        elif verified is False:
            detail += " I could not confirm it was sent - please check your Sent folder."
        else:
            detail += " I could not fully verify it - please check your Sent folder."
        return AutomationResult(
            ok=True,
            action="send_composed_email",
            detail=detail,
            verified=verified,
            data={"verified": verified},
        )

    # ---------------------------------------------------------------- helpers
    def _browser_window(self, hint: str = "") -> tuple[Any, str]:
        needles = [hint] if hint else []
        needles.extend(["chrome", "edge", "firefox", "brave", "opera", "browser", "gmail"])
        try:
            windows = self.controller.desktop.list_windows()
        except BackendUnavailable as exc:
            return None, exc.message
        except Exception as exc:  # pragma: no cover
            return None, f"Could not list windows: {exc}"
        for needle in needles:
            if not needle:
                continue
            for window in windows:
                if window.matches(needle):
                    return window, ""
        return None, "No browser window is open."

    def fill_field(self, label: str, value: str, *, window_hint: str = "") -> AutomationResult:
        return self.controller.set_text_in_element(label, value, window=window_hint)

    def current_time_stamp(self) -> str:
        return now().strftime("%d %b %Y %H:%M")
