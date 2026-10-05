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
    expect(page).to_have_title("Research Workspace · Research messaging")
    page.locator("#use-token").click()
    page.locator("#token-input").fill(credentials["token"])
    page.locator("#signin-form").get_by_role("button", name="Continue").click()
    expect(page.locator("#workspace-view")).to_be_visible()
    expect(page.locator("#connection-status")).to_contain_text("Connected")
    if page.locator("#modal").is_visible():
        expect(page.locator("#modal-title")).to_have_text("My account")
        page.locator("#modal").get_by_role("button", name="Close", exact=True).click()
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
    # A single reply is expandable and replying from it still targets the root.
    root = page.locator(".message-group").first
    root.locator(".message > .message-main > .message-actions > .reply-button").first.click()
    page.locator("#message-input").fill("First reply")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(root.locator(".replies-toggle")).to_have_text("Hide 1 reply")
    expect(root.locator(".message-replies .message-text")).to_have_text("First reply")
    root.locator(".replies-toggle").click()
    expect(root.locator(".message-replies")).not_to_be_visible()
    root.locator(".replies-toggle").click()
    root.locator(".message-replies .reply-button").click()
    page.locator("#message-input").fill("Second reply to root")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(root.locator(".replies-toggle")).to_have_text("Hide 2 replies")
    expect(root.locator(".message-replies .message")).to_have_count(2)
    reactions = root.locator(".message-reactions").first
    reaction = reactions.get_by_role("button", name="Add 👍 reaction", exact=True)
    reaction.click()
    expect(reactions.locator('[aria-pressed="true"]')).to_have_text("👍 1")
    root.locator(".message-reactions").first.locator('[aria-pressed="true"]').click()
    expect(root.locator(".message-reactions").first.locator('[aria-pressed="true"]')).to_have_count(0)
    expect(root.locator(".message-text").first).to_contain_text("Browser message with evidence")
    long_text = "<img src=x onerror=alert(1)> " + "evidence " * 120
    page.locator("#message-input").fill(long_text)
    page.get_by_role("button", name="Send message", exact=True).click()
    long_message = page.locator(".message-group").last
    expect(long_message.locator(".message-text")).to_have_text(long_text[:800] + "…")
    expect(long_message.locator("img")).to_have_count(0)
    long_message.locator(".detail-toggle").click()
    expect(long_message.locator(".message-text")).to_have_text(long_text)
    page.locator("#new-thread").click()
    page.locator('#modal [name="title"]').fill("Second review")
    page.locator("#modal").get_by_role("button", name="Start conversation", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    expect(page.locator("#thread-title")).to_have_text("Second review")
    page.locator("#thread-select").select_option(label="First review")
    expect(page.locator("#thread-title")).to_have_text("First review")
    expect(page.locator(".message-text").first).to_have_text("Browser message with evidence")
    page.locator("#members-open").click()
    page.locator("#modal").get_by_role("button", name="Create an agent", exact=True).click()
    page.locator('#modal [name="name"]').fill("Browser agent")
    page.locator('#modal [name="handle"]').fill("browser-agent")
    page.locator("#modal").get_by_role("button", name="Create agent", exact=True).click()
    expect(page.locator("#modal-title")).to_have_text("Agent created")
    expect(page.locator(".token-reveal code")).not_to_be_empty()
    page.locator("#modal").get_by_role("button", name="Done", exact=True).click()
    agent_option = page.locator(".inline-form select option", has_text="Browser agent")
    expect(agent_option).to_contain_text("owned by")
    page.locator(".inline-form select").select_option(value=agent_option.get_attribute("value"))
    page.locator("#modal").get_by_role("button", name="Add participant", exact=True).click()
    expect(page.locator(".member-name", has_text="Browser agent")).to_be_visible()
    agent_details = page.locator(".member-row", has_text="Browser agent").locator(".member-sub")
    expect(agent_details).to_contain_text("owned by")
    page.get_by_role("button", name="Actions for Browser agent").click()
    page.locator("#modal").get_by_role("button", name="Mute", exact=True).click()
    muted_chip = page.locator(".member-row", has_text="Browser agent").locator(".chip")
    expect(muted_chip).to_have_text("Muted")
    page.get_by_role("button", name="Actions for Browser agent").click()
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
                "single reply expansion and root targeting",
                "reaction toggle",
                "long text and XSS safety",
                "agent handles and ownership",
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
