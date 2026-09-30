#!/usr/bin/env python3
"""Exercise the actual browser interface against a running local pilot."""

import argparse
import json
import shutil
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8000")
parser.add_argument("--credentials", required=True)
parser.add_argument("--screenshot", default=".local/browser-smoke.png")
args = parser.parse_args()
credentials = json.loads(Path(args.credentials).read_text())
errors = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=shutil.which("chromium"))
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(args.url)
    page.locator("#token-input").fill(credentials["token"])
    page.locator("#signin-form").get_by_role("button", name="Continue").click()
    expect(page.locator("#workspace-view")).to_be_visible()
    page.locator("#new-project").click()
    page.locator('#modal [name="name"]').fill("Browser research check")
    page.locator('#modal [name="description"]').fill("Live interface verification")
    page.locator("#modal").get_by_role("button", name="Create project", exact=True).click()
    expect(page.locator("#project-crumb")).to_have_text("Browser research check")
    page.locator("#new-channel").click()
    page.locator('#modal [name="name"]').fill("analysis")
    page.locator("#modal").get_by_role("button", name="Create channel", exact=True).click()
    page.locator("#welcome-thread").click()
    page.locator('#modal [name="title"]').fill("First review")
    page.locator("#modal").get_by_role("button", name="Start conversation", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    expect(page.locator("#thread-title")).to_have_text("First review")
    page.locator("#message-input").fill("Browser message with evidence")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".message-text")).to_have_text("Browser message with evidence")
    page.locator("#new-thread").click()
    page.locator('#modal [name="title"]').fill("Second review")
    page.locator("#modal").get_by_role("button", name="Start conversation", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    expect(page.locator("#thread-title")).to_have_text("Second review")
    page.locator("#thread-select").select_option(label="First review")
    expect(page.locator("#thread-title")).to_have_text("First review")
    expect(page.locator(".message-text")).to_have_text("Browser message with evidence")
    page.locator("#members-open").click()
    page.locator("#modal").get_by_role("button", name="Create an agent", exact=True).click()
    page.locator('#modal [name="name"]').fill("Browser agent")
    page.locator("#modal").get_by_role("button", name="Create agent", exact=True).click()
    expect(page.locator("#modal-title")).to_have_text("Agent created")
    expect(page.locator(".token-reveal code")).not_to_be_empty()
    page.locator("#modal").get_by_role("button", name="Done", exact=True).click()
    page.locator(".inline-form select").select_option(label="Browser agent · agent")
    page.locator("#modal").get_by_role("button", name="Add person", exact=True).click()
    expect(page.locator(".member-name", has_text="Browser agent")).to_be_visible()
    page.locator("#modal").get_by_role("button", name="Mute", exact=True).click()
    expect(page.locator("#modal").get_by_role("button", name="Unmute", exact=True)).to_be_visible()
    page.locator("#modal").get_by_role("button", name="Close", exact=True).click()
    page.locator("#rules-open").click()
    page.locator('#modal [name="rules"]').fill("State units and link reproducible evidence.")
    page.locator("#modal").get_by_role("button", name="Save rules", exact=True).click()
    expect(page.locator(".modal-alert")).to_have_text("Rules saved.")
    page.locator("#modal").get_by_role("button", name="Close", exact=True).click()
    Path(args.screenshot).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=args.screenshot, full_page=True)
    page.locator("#signout").click()
    expect(page.locator("#signin-view")).to_be_visible()
    assert page.locator("#token-input").input_value() == ""
    assert not errors, f"Browser runtime errors: {errors}"
    browser.close()
print(
    json.dumps(
        {
            "browser_checks": "passed",
            "checks": [
                "sign-in",
                "navigation",
                "posting",
                "thread switching",
                "one-time token display",
                "membership",
                "moderation",
                "rules",
                "sign-out",
            ],
            "screenshot": args.screenshot,
        }
    )
)
