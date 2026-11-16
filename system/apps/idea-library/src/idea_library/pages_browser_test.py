"""The Idea Library's pages, driven in a real browser against a throwaway server over the synthetic library.

Every request to anywhere but the throwaway server (the reading page's Google Fonts) is aborted, so
nothing here reaches the internet. Each page records every Content-Security-Policy violation, and
every test fails if one fired: the policy is computed from the pages, and a block would silently
break a page.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120, func_only=False)]

RECORD_POLICY_VIOLATIONS = """
window.__violations = [];
document.addEventListener('securitypolicyviolation', e => window.__violations.push(e.violatedDirective + ' ' + e.blockedURI));
"""


@pytest.fixture
def page(module_browser: Browser, library_url: str) -> Iterator[Page]:
    context = module_browser.new_context(viewport={"width": 1400, "height": 900})
    context.add_init_script(RECORD_POLICY_VIOLATIONS)
    browser_page = context.new_page()
    browser_page.route(
        "**/*",
        lambda route: route.continue_()
        if route.request.url.startswith(library_url)
        else route.abort(),
    )
    try:
        yield browser_page
        assert browser_page.evaluate("window.__violations") == []
    finally:
        context.close()


def _open_reading_page(page: Page, url: str) -> None:
    page.goto(url)
    expect(page.locator(".stat")).to_be_visible()


def test_the_reading_page_lists_every_chapter_in_the_library_order(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    expect(page.locator(".stat")).to_have_text("6 ideas · 3 connections · 3 sources")
    expect(page.locator("main .sec-title")).to_have_text(
        ["Support & belonging", "Awareness & inquiry", "Rest & renewal"]
    )
    spine = page.locator("#rail .toc a")
    expect(spine).to_have_count(3)
    expect(spine.nth(2)).to_contain_text("what restores us · 3 ideas")
    rest = page.locator("main .sec").nth(2)
    expect(rest.locator(".etitle")).to_have_text(
        ["Recovery is a practice", "Tiredness is a signal", "Habits loop back"]
    )



def test_the_reading_page_says_so_when_the_library_cannot_load(
    page: Page, library_url: str, library_dir: Path
) -> None:
    (library_dir / "map-data.json").write_text("{not json")
    page.goto(library_url + "/")
    expect(page.locator("#load-error")).to_have_text(
        "The library could not load. Refresh to try again."
    )


def test_a_card_opens_in_place_with_quotes_linked_to_their_sources(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    card = page.locator("#e-lantern-light")
    expect(card).not_to_have_attribute("open", "")
    card.locator("summary").click()
    expect(card).to_have_attribute("open", "")
    quote = card.locator(".q")
    expect(quote).to_contain_text("A lantern only shows the next step.")
    expect(quote.locator("cite a")).to_have_attribute(
        "href", "https://example.com/recordings/lantern"
    )
    # Connections this idea makes come first, then the ones that point at it; then its sources.
    expect(card.locator("ul.links").first.locator("li")).to_have_text(
        [
            "This may help explain Orchards keep seasonssuggested link",
            "Tides always return leads to this",
        ]
    )
    expect(card.locator("ul.links").nth(1).locator("a")).to_have_attribute(
        "href", "https://example.com/recordings/lantern"
    )
    expect(card.locator(".sg")).to_have_text("suggested link")
    expect(card.locator("a.atlas")).to_have_attribute("href", "map.html#lantern-light")


def test_the_threads_tab_ranks_ideas_by_connections(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    page.locator('.tab[data-v="threads"]').click()
    expect(page.locator('.tab[data-v="threads"]')).to_have_class("tab on")
    expect(page.locator("main .sec-title")).to_have_text(["Most connected first"])
    expect(page.locator("main .sec-sub")).to_have_text("3 connections")
    expect(page.locator("details.entry .etitle").first).to_have_text(
        "A lantern lights one step"
    )


def test_the_sources_tab_groups_ideas_under_each_source_with_its_original(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    page.locator('.tab[data-v="sources"]').click()
    expect(page.locator("main .sec-title")).to_have_text(
        ["Notes on Tides", "Voice note: the lantern", "Recording: orchard walk"]
    )
    tides = page.locator("main .sec").first
    expect(tides.locator(".blurb a")).to_have_attribute(
        "href", "https://example.com/p/notes-on-tides"
    )
    expect(tides.locator(".etitle")).to_have_text(
        ["Tides always return", "Recovery is a practice", "Habits loop back"]
    )
    tides.locator("details.raw summary").click()
    expect(tides.locator("details.raw pre")).to_have_text(
        "Full saved text of Notes on Tides. The tide comes back every evening."
    )


def test_search_filters_and_highlights_and_the_keys_drive_it(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    page.locator("body").press("/")
    expect(page.locator("#q")).to_be_focused()
    page.keyboard.type("lantern")
    expect(page.locator("details.entry")).to_have_count(1)
    expect(page.locator("details.entry .etitle")).to_have_text(
        "A lantern lights one step"
    )
    expect(page.locator(".stat")).to_contain_text("1 match “lantern”")
    expect(page.locator("main mark").first).to_have_text("lantern")
    # A handful of matches open so the highlighted passages show.
    expect(page.locator("details.entry")).to_have_attribute("open", "")
    expect(page.locator("#rail .toc a.dim")).to_have_count(2)
    expect(page.locator("#clear")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#q")).to_have_value("")
    expect(page.locator("details.entry")).to_have_count(6)
    page.locator("#q").fill("no idea says this")
    expect(page.locator(".empty")).to_have_text(
        "Nothing matches that search in this view."
    )


def test_a_deep_link_opens_and_marks_its_card(page: Page, library_url: str) -> None:
    page.goto(library_url + "/#orchard-seasons")
    card = page.locator("#e-orchard-seasons")
    expect(card).to_have_attribute("open", "")
    expect(card).to_have_class("entry target")
    expect(page.locator("details.entry[open]")).to_have_count(1)
    # Following a connection link moves the deep link to that idea.
    card.locator("[data-goto]").first.click()
    expect(page.locator("#e-lantern-light")).to_have_class("entry target")
    assert page.evaluate("location.hash") == "#lantern-light"


def test_the_map_selects_the_idea_named_in_its_link(
    page: Page, library_url: str
) -> None:
    page.goto(library_url + "/map.html#orchard-seasons")
    expect(page.locator("#counts")).to_have_text("6 of 6 ideas · 3 connections")
    panel = page.locator("#panel")
    expect(panel).to_be_visible()
    expect(panel.locator("h2")).to_have_text("Orchards keep seasons")
    expect(page.locator("#reading-link")).to_have_attribute(
        "href", "textbook.html#orchard-seasons"
    )
    expect(page.locator('[data-node="orchard-seasons"] rect')).to_have_attribute(
        "stroke", "#345a3d"
    )
    # The transcript and the original are both reachable from the selected idea's source.
    expect(panel.locator('a[href="map-assets/transcripts/orchard.txt"]')).to_have_count(
        1
    )
    expect(
        panel.locator('a[href="https://example.com/recordings/orchard"]')
    ).to_have_count(1)


def test_the_map_plays_a_voice_note_from_the_library(
    page: Page, library_url: str
) -> None:
    page.goto(library_url + "/map.html#lantern-light")
    audio = page.locator("#panel audio")
    expect(audio).to_have_attribute("src", "map-assets/lantern.mp3")
    status = page.evaluate("fetch('map-assets/lantern.mp3').then(r => r.status)")
    assert status == 200


def test_the_classic_view_shows_the_first_sample(page: Page, library_url: str) -> None:
    page.goto(library_url + "/classic.html")
    expect(page.locator("#grid .card")).to_have_count(2)
    expect(page.locator(".metrics b")).to_have_text(["2", "1", "2"])
    expect(page.locator("#results")).to_have_text("2 ideas")
    page.locator('[data-idea="idea-1"]').click()
    expect(page.locator("#drawer h2")).to_have_text("Patience with tides")
    expect(page.locator("#drawer a.source-link")).to_have_attribute(
        "href", "https://example.com/p/notes-on-tides"
    )


def test_the_reading_page_links_to_the_map_and_the_classic_view(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    page.locator("a.tl", has_text="Classic view").click()
    expect(page).to_have_url(library_url + "/classic.html")
    expect(page.locator("#grid .card")).to_have_count(2)
    page.goto(library_url + "/")
    page.locator("a.tl", has_text="Idea map").click()
    expect(page).to_have_url(library_url + "/map.html")
    expect(page.locator("#counts")).to_have_text("6 of 6 ideas · 3 connections")


def test_the_map_shows_each_chapter_as_its_own_group(
    page: Page, library_url: str
) -> None:
    page.goto(library_url + "/map.html#habit-loop")
    expect(page.locator("#counts")).to_have_text("6 of 6 ideas · 3 connections")
    panel = page.locator("#panel")
    expect(panel.locator("h2")).to_have_text("Habits loop back")
    expect(page.locator("#legend")).to_contain_text("Rest & renewal")
    page.locator("#group-filter").select_option("Rest & renewal")
    expect(page.locator("#counts")).to_have_text("3 of 6 ideas · 1 connections")



def test_the_essay_seed_label_is_plain_text_not_a_gold_box(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    label = page.locator("#e-habit-loop .etype")
    expect(label).to_have_text("Idea · essay seed")
    assert (
        label.evaluate("el => getComputedStyle(el).backgroundColor")
        == "rgba(0, 0, 0, 0)"
    )
    page.locator("#e-habit-loop summary").click()
    box = page.locator("#e-habit-loop .essay-seed")
    expect(box).to_have_count(1)
    assert (
        box.evaluate("el => getComputedStyle(el).backgroundColor") != "rgba(0, 0, 0, 0)"
    )


def test_the_graph_draws_every_idea_and_opens_one_with_its_sources(
    page: Page, library_url: str
) -> None:
    page.goto(library_url + "/graph.html")
    expect(page.locator("#hud")).to_contain_text("6 ideas")
    expect(page.locator("#hud")).to_contain_text("3 connections")
    expect(page.locator("#legend .chip")).to_have_count(3)
    page.goto(library_url + "/graph.html#habit-loop")
    page.reload()
    panel = page.locator("#panel")
    expect(panel).to_have_class("panel open")
    expect(panel.locator("h2")).to_have_text("Habits loop back")
    expect(panel.locator('a[href="textbook.html#habit-loop"]')).to_have_count(1)
    expect(panel.locator('a[href="map.html#habit-loop"]')).to_have_count(1)
    page.keyboard.press("Escape")
    expect(panel).not_to_have_class("panel open")


def test_the_graph_toggles_the_sphere_and_a_chapter_can_be_hidden(
    page: Page, library_url: str
) -> None:
    page.goto(library_url + "/graph.html")
    expect(page.locator("#hud")).to_contain_text("6 ideas")
    page.locator("#sphere").click()
    expect(page.locator("#hud")).to_contain_text("sphere")
    page.locator("#sphere").click()
    page.locator('.chip[data-g="Rest & renewal"]').click()
    expect(page.locator("#hud")).to_contain_text("3 ideas")
    expect(page.locator('.chip[data-g="Rest & renewal"]')).to_have_class(
        "chip off"
    )



def test_the_reading_page_and_the_map_link_to_the_graph(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    page.locator("a.tl", has_text="Graph").click()
    expect(page).to_have_url(library_url + "/graph.html")
    page.goto(library_url + "/map.html")
    expect(page.locator('a[href="graph.html"]')).to_have_count(1)


def _own_library(library_dir: Path) -> None:
    """Turn the sample into someone else's library: their name, their chapters, one more chapter than the palette."""
    path = library_dir / "map-data.json"
    data = json.loads(path.read_text())
    data["nodes"] = data["nodes"][:4]
    kept = {node["id"] for node in data["nodes"]}
    data["edges"] = [e for e in data["edges"] if e["source"] in kept and e["target"] in kept]
    names = ["Bees and patience", "Night walking"]
    for index, node in enumerate(data["nodes"]):
        node["group"] = names[index % 2]
    data["author"] = "Robin"
    data["collection"] = "Hive Notes"
    data["reading_intro"] = "From the hive outward."
    data["chapters"] = [
        {"name": "Bees and patience", "subtitle": "waiting as a skill"},
        {"name": "Night walking", "subtitle": "what the dark shows"},
    ]
    path.write_text(json.dumps(data))


def test_the_reading_page_shows_the_owner_and_their_own_chapters(
    page: Page, library_url: str, library_dir: Path
) -> None:
    _own_library(library_dir)
    _open_reading_page(page, library_url + "/")
    expect(page.locator("main")).to_contain_text("Robin’s collected thinking")
    expect(page.locator("main")).to_contain_text("From the hive outward.")
    expect(page.locator("main .sec-title")).to_have_text(
        ["Bees and patience", "Night walking"]
    )
    expect(page.locator("main .sec-sub").first).to_have_text("waiting as a skill")


def test_a_library_with_no_owner_name_reads_as_collected_thinking(
    page: Page, library_url: str
) -> None:
    _open_reading_page(page, library_url + "/")
    expect(page.locator("main")).to_contain_text("Collected thinking")
    expect(page.locator("main")).not_to_contain_text("’s collected thinking")


def test_the_map_colours_and_filters_chapters_it_has_never_seen(
    page: Page, library_url: str, library_dir: Path
) -> None:
    _own_library(library_dir)
    page.goto(library_url + "/map.html")
    expect(page.locator("#counts")).to_have_text("4 of 4 ideas · 2 connections")
    expect(page.locator("#collection-name")).to_have_text("Hive Notes")
    expect(page.locator("#legend")).to_contain_text("Night walking")
    page.locator("#group-filter").select_option("Night walking")
    expect(page.locator("#counts")).to_have_text("2 of 4 ideas · 0 connections")
    fills = page.locator("[data-node] rect").evaluate_all(
        "els => [...new Set(els.map(e => e.getAttribute('fill')))]"
    )
    assert fills == ["#f2e6d9"]


def test_the_map_offers_each_conversation_group_in_the_library(
    page: Page, library_url: str
) -> None:
    page.goto(library_url + "/map.html")
    conversation = page.locator("#conversation-filter")
    expect(conversation).to_be_visible()
    conversation.select_option("Garden conversations")
    # Two ideas quote the orchard recording.
    expect(page.locator("#counts")).to_have_text("2 of 6 ideas · 0 connections")


def test_the_map_hides_the_conversation_filter_when_there_are_none(
    page: Page, library_url: str, library_dir: Path
) -> None:
    path = library_dir / "map-data.json"
    data = json.loads(path.read_text())
    for source in data["sources"]:
        source.pop("conversation_family", None)
    path.write_text(json.dumps(data))
    page.goto(library_url + "/map.html")
    expect(page.locator("#counts")).to_have_text("6 of 6 ideas · 3 connections")
    expect(page.locator("#conversation-filter")).to_be_hidden()


def test_the_graph_gives_each_new_chapter_its_own_colour(
    page: Page, library_url: str, library_dir: Path
) -> None:
    _own_library(library_dir)
    page.goto(library_url + "/graph.html")
    expect(page.locator("#hud")).to_contain_text("4 ideas")
    swatches = page.locator("#legend .chip i").evaluate_all(
        "els => els.map(e => e.style.background)"
    )
    assert swatches == ["rgb(110, 150, 96)", "rgb(196, 146, 92)"]


def test_the_classic_view_shows_the_owner_when_the_sample_names_one(
    page: Page, library_url: str, library_dir: Path
) -> None:
    path = library_dir / "sample.json"
    data = json.loads(path.read_text())
    data.update(author="Robin", collection="Hive Notes", note="Recordings come next.")
    path.write_text(json.dumps(data))
    page.goto(library_url + "/classic.html")
    expect(page.locator("#grid .card")).to_have_count(2)
    expect(page.locator("#brand-sub")).to_have_text("Robin’s collected thinking")
    expect(page.locator("#topline-label")).to_have_text(
        "HIVE NOTES / YOUR THINKING, CONNECTED"
    )
    expect(page.locator("#sample-note")).to_have_text("Recordings come next.")


NO_LIBRARY = "No library yet. Ask your agent to build your Idea Library from your writing and recordings."


def _no_library(library_dir: Path) -> None:
    for name in ("map-data.json", "sample.json"):
        (library_dir / name).unlink()


def test_every_page_says_to_build_the_library_when_there_is_none(
    page: Page, library_url: str, library_dir: Path
) -> None:
    _no_library(library_dir)
    page.goto(library_url + "/")
    expect(page.locator("#load-error")).to_have_text(NO_LIBRARY)
    page.goto(library_url + "/map.html")
    expect(page.locator("#counts")).to_have_text(NO_LIBRARY)
    page.goto(library_url + "/graph.html")
    expect(page.locator("#empty")).to_have_text(NO_LIBRARY)
    expect(page.locator("#empty")).to_be_visible()
    page.goto(library_url + "/classic.html")
    expect(page.locator("#grid .empty")).to_have_text(NO_LIBRARY)
    expect(page.locator(".metrics b")).to_have_text(["0", "0", "0"])
    expect(page.locator(".footer")).not_to_contain_text("seven saved articles")
