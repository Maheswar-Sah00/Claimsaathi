"""Smoke test: open a Browserbase cloud browser, load a page, print the replay URL."""
import os

from browserbase import Browserbase
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

bb = Browserbase(api_key=os.environ["BROWSERBASE_API_KEY"])
session = bb.sessions.create(project_id=os.environ["BROWSERBASE_PROJECT_ID"])

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(session.connect_url)
    page = browser.contexts[0].pages[0]
    page.goto("https://example.com")
    print("title:", page.title())
    page.close()
    browser.close()

print("replay:", f"https://www.browserbase.com/sessions/{session.id}")
