"""Buyer title rules (live_buyers.title_rank) and freight job-post filter
(live_signals.is_relevant_logistics_job_title)."""
import pytest

from service.config import settings
from service.sources.live_buyers import title_matches, title_rank
from service.sources.live_signals import is_relevant_logistics_job_title as relevant

TITLES = settings()["buyer"]["titles"]


@pytest.mark.parametrize("title", [
    "Head of Information Technology - Supply chain, Finance & Corporate",   # live, L'Oreal
    "Head of Supply Chain Finance - Global Emerging Markets (GEMS)",        # live, General Mills
    "VP Supply Chain - IT", "Director, Supply Chain Quality", "Head of Logistics Sales",
])
def test_non_buyer_functions_never_match(title):
    assert not title_matches(title, TITLES)


@pytest.mark.parametrize("title", [
    "Head of Supply Chain", "Regional Logistics Manager", "Logistics Operations Manager",
    "Head of Operations", "Chief Operating Officer", "COO",
])
def test_freight_buyers_match(title):
    assert title_matches(title, TITLES)


def test_junior_titles_rank_after_senior_ones():
    assert title_rank("Head of Operations", TITLES) < title_rank("Asst Mgr-Import Logistics", TITLES)
    assert title_matches("Asst Mgr-Import Logistics", TITLES)       # still a buyer, just last


@pytest.mark.parametrize("title,ok", [
    ("HR Generalist – Warehouse HR & Employee Relations", False),   # live, KALKI
    ("Warehouse Accountant", False), ("Logistics Sales Executive", False),
    ("Admin - Visa & Travel for Logistics Industry", False),
    ("Warehouse Manager", True), ("Supply Chain Buyer", True), ("Fleet Manager", True),
    ("Materials Manager - SCM & Logistics", True), ("Warehousing Analyst", True),
])
def test_freight_job_posts(title, ok):
    assert relevant(title) is ok
