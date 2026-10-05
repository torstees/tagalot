"""Research actions and citations by hand (#322): Copy citation, Export BibTeX…, Open DOI
page; Cites / Cited by (a relationship of Paper with itself, with a direction) and Related
(symmetric)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tagalot.builtin_themes.research import (
    Paper,
    ResearchTheme,
    bibtex_entry,
    citation,
    initials,
    paper_url,
)
from tagalot.core.actions import ActionError, run_action
from tagalot.core.detail import load_detail
from tagalot.core.relations import RelationError, add_related, remove_related
from tagalot.core.roots import place_in_roots
from tagalot.core.search import run_search
from tagalot.core.search_fields import search_fields
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.themes.api import ThemeDeclarationError, related
from tagalot.themes.bibliography import parse_bibtex
from tests.core.book_files import write_text_pdf
from tests.themes.test_research import Env
from tests.themes.test_research_bibliography import env

__all__ = ["env"]  # the fixture

EMPTY = TagTree([], {})


def _record(title: str, **fields: Any) -> Any:
    return SimpleNamespace(title=title, fields=fields)


ATTENTION = _record(
    "Attention Is All You Need",
    year=2017,
    venue="Advances in Neural Information Processing Systems",
    volume="30",
    pages="5998-6008",
    arxiv="1706.03762",
    kind="conference paper",
    citekey="vaswani2017",
)


def test_citation() -> None:
    assert initials("Ashish Vaswani") == "Vaswani, A."
    assert initials("Jean-Paul Sartre") == "Sartre, J. P."
    text = citation(ATTENTION, ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"])
    assert text == (
        "Vaswani, A., Shazeer, N., & Parmar, N. (2017). Attention Is All You Need. "
        "Advances in Neural Information Processing Systems, 30, 5998-6008. "
        "https://arxiv.org/abs/1706.03762"
    )
    assert citation(_record("Untitled draft."), []) == "(n.d.). Untitled draft."


def test_paper_urls() -> None:
    assert paper_url({"doi": "10.1/x", "arxiv": "1"}) == "https://doi.org/10.1/x"
    assert paper_url({"arxiv": "1706.03762"}) == "https://arxiv.org/abs/1706.03762"
    assert paper_url({"url": "ftp://x"}) is None


def test_bibtex_round_trips_through_the_reader() -> None:
    keys: set[str] = set()
    text = bibtex_entry(ATTENTION, ["Ashish Vaswani", "Noam Shazeer"], keys)
    text += bibtex_entry(_record("R&D at 50%", year=2020), ["Ann Lee"], keys)
    text += bibtex_entry(_record("R&D at 50% again", year=2020), ["Ann Lee"], keys)
    entries = {e["citekey"]: e for e in parse_bibtex(text)}
    assert set(entries) == {"vaswani2017", "lee2020r", "lee2020ra"}  # made unique
    attention = entries["vaswani2017"]
    assert attention["type"] == "inproceedings"
    assert (attention["title"], attention["authors"]) == (
        "Attention Is All You Need",
        ["Ashish Vaswani", "Noam Shazeer"],
    )
    assert (attention["venue"], attention["pages"], attention["arxiv"]) == (
        "Advances in Neural Information Processing Systems",
        "5998-6008",
        "1706.03762",
    )
    assert entries["lee2020r"]["title"] == "R&D at 50%"  # escaped, and read back


# --- running the actions ---


def _papers(env: Env) -> tuple[int, int]:
    write_text_pdf(
        env.files / "Vaswani et al. - 2017 - Attention Is All You Need.pdf",
        ["Attention", "arXiv:1706.03762"],
        Author="Ashish Vaswani; Noam Shazeer",
    )
    write_text_pdf(
        env.files / "He et al. - 2016 - Deep Residual Learning.pdf",
        ["ResNet", "doi:10.1109/CVPR.2016.90"],
    )
    env.scan()
    return env.id_of("Attention Is All You Need"), env.id_of("Deep Residual Learning")


def _run(env: Env, method: str, ids: list[int]) -> Any:
    def job(conn: Any) -> Any:
        return run_action(
            conn,
            env.schema,
            ResearchTheme(),
            method,
            ids,
            root_path=lambda _: str(env.files),
            temp_dir=lambda: env.files,
        )

    return env.writer.run(job)


def test_the_actions_give_their_outputs(env: Env) -> None:
    attention, resnet = _papers(env)
    result = _run(env, "copy_citation", [attention, resnet])
    [copied] = [o for o in result.outputs if o[0] == "copy"]
    assert copied[1].startswith("Vaswani, A., & Shazeer, N. (2017). Attention Is All You Need.")
    # "He" alone, from its file name.
    assert copied[1].endswith(
        "\n\nHe (2016). Deep Residual Learning. https://doi.org/10.1109/cvpr.2016.90"
    )
    assert result.text == "Copied 2 citations."
    result = _run(env, "export_bibtex", [attention])
    [saved] = [o for o in result.outputs if o[0] == "save"]
    assert saved[1].endswith(".bib")
    assert parse_bibtex(saved[2])[0]["title"] == "Attention Is All You Need"
    result = _run(env, "open_doi_page", [attention, resnet])
    assert [o[1] for o in result.outputs if o[0] == "url"] == [
        "https://arxiv.org/abs/1706.03762",
        "https://doi.org/10.1109/cvpr.2016.90",
    ]


def test_action_outputs_are_checked() -> None:
    from tagalot.core.actions import ActionSession

    ctx = ActionSession.__new__(ActionSession)
    ctx.outputs = []
    with pytest.raises(ActionError, match="Only web addresses"):
        ctx.open_url("file:///etc/passwd")
    with pytest.raises(ActionError, match="without folders"):
        ctx.save_text("../x.bib", "text")
    ctx.open_url(" https://doi.org/10.1/x ")
    ctx.save_text("x.bib", "@misc{x}")
    ctx.copy_text("hello")
    assert ctx.outputs == [
        ("url", "https://doi.org/10.1/x"),
        ("save", "x.bib", "@misc{x}"),
        ("copy", "hello"),
    ]


# --- Cites, Cited by, Related ---


def test_symmetric_needs_one_type() -> None:
    from tagalot.builtin_themes.research import Author

    with pytest.raises(ThemeDeclarationError, match="relates a type with itself"):
        related("x", Author, Paper, symmetric=True)


def _sections(env: Env, paper: int) -> dict[str, list[str]]:
    detail = env.writer.run(lambda conn: load_detail(conn, env.schema, paper, lambda _: None))
    assert detail is not None
    return {s.title: [e.title for e in s.entities] for s in detail.sections if s.kind == "related"}


def test_cites_has_a_direction_and_related_has_none(env: Env) -> None:
    attention, resnet = _papers(env)
    env.writer.run(lambda conn: add_related(conn, env.schema, "cites", attention, resnet, side="a"))
    env.writer.run(
        lambda conn: add_related(conn, env.schema, "related", resnet, attention, side=None)
    )
    assert _sections(env, attention) == {
        "Authors": ["Ashish Vaswani", "Noam Shazeer"],
        "Cites": ["Deep Residual Learning"],
        "Cited by": [],
        "Related": ["Deep Residual Learning"],
    }
    assert _sections(env, resnet) == {
        "Authors": ["He"],
        "Cites": [],
        "Cited by": ["Attention Is All You Need"],
        "Related": ["Attention Is All You Need"],  # either way
    }
    # Adding to "Cited by" makes the other paper cite this one.
    other = env.writer.run(
        lambda conn: add_related(
            conn, env.schema, "cites", attention, new_title="A Survey", side="b"
        )
    )
    assert other.label == "Add 'A Survey' to Attention Is All You Need's cited by"
    assert _sections(env, attention)["Cited by"] == ["A Survey"]
    with pytest.raises(RelationError, match="can't be added"):
        env.writer.run(
            lambda conn: add_related(conn, env.schema, "cites", attention, attention, side="a")
        )


def test_searches_and_removal_by_side(env: Env) -> None:
    attention, resnet = _papers(env)
    env.writer.run(lambda conn: add_related(conn, env.schema, "cites", attention, resnet, side="a"))
    fields = search_fields(env.schema, ("research.paper",))

    def titles(paper: int, side: str | None) -> list[str]:
        spec = SearchSpec(types=("research.paper",), related=("cites", paper), related_side=side)
        with env.reader.connect() as conn:
            return [h.title for h in run_search(conn, spec, EMPTY, fields=fields)]

    assert titles(attention, "a") == ["Deep Residual Learning"]
    assert titles(attention, "b") == []
    assert titles(resnet, "b") == ["Attention Is All You Need"]
    spec = SearchSpec(related=("cites", attention), related_side="a")
    assert SearchSpec.from_json(spec.to_json()) == spec
    with pytest.raises(RelationError, match="isn't related"):  # not on that side
        env.writer.run(
            lambda conn: remove_related(conn, env.schema, "cites", attention, [resnet], "b")
        )
    env.writer.run(lambda conn: remove_related(conn, env.schema, "cites", resnet, [attention], "b"))
    assert titles(attention, "a") == []


# --- where an export may be saved ---


def test_place_in_roots(tmp_path: Path) -> None:
    roots = {"r": str(tmp_path / "Papers"), "s": None}
    assert place_in_roots(str(tmp_path / "Papers" / "a" / "x.bib"), roots) == ("r", "a/x.bib")
    assert place_in_roots(str(tmp_path / "Elsewhere" / "x.bib"), roots) is None
    assert place_in_roots(str(tmp_path / "Papers"), roots) is None  # the root itself
    assert place_in_roots(str(tmp_path / "Papers2" / "x.bib"), roots) is None  # not a prefix
