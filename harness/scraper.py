"""
Scrape event cards off the logged-in home page.

Cards render as `a[href*="/event/"]` links via EventCard.tsx. We extract title
/ datetime / location by splitting innerText on newlines — brittle but matches
the current DOM and isolated here so it's easy to replace.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page


def scrape_home_events(page: Page) -> list[dict]:
    for _ in range(3):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
    page.evaluate("window.scrollTo(0, 0)")

    return page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a[href*="/event/"]'));
            return links.map(el => {
                const lines = el.innerText.trim().split('\\n')
                    .map(l => l.trim())
                    .filter(l => l && l !== 'No image');
                return {
                    title: lines[0] || '',
                    datetime: lines[1] || '',
                    location: lines[2] || '',
                };
            }).filter(e => e.title);
        }"""
    )


def scrape_my_rsvps(page: Page) -> list[dict]:
    """Events in the 'You're attending' / 'My RSVPs' section."""
    return page.evaluate(
        """() => {
            const headings = Array.from(
                document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,div')
            ).filter(el =>
                /you'?re attending|my rsvps|going to/i.test(el.innerText.trim())
                && el.innerText.trim().length < 80
            );
            for (const heading of headings) {
                const container = heading.closest('section') || heading.parentElement;
                if (!container) continue;
                const links = Array.from(container.querySelectorAll('a[href*="/event/"]'));
                if (links.length) {
                    return links.map(el => {
                        const lines = el.innerText.trim().split('\\n')
                            .map(l => l.trim())
                            .filter(l => l && l !== 'No image');
                        return {
                            title: lines[0] || '',
                            datetime: lines[1] || '',
                            location: lines[2] || '',
                        };
                    }).filter(e => e.title);
                }
            }
            return [];
        }"""
    )


def format_events(events: list[dict]) -> str:
    if not events:
        return "(none)"
    return "\n".join(
        f"- {e['title']} | {e['datetime']} | {e['location']}" for e in events
    )
