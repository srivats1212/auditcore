import pytest

from auditcore.tagging.rules import sentence_is_finding, tag_text
from auditcore.taxonomy.loader import TaxonomyError, parse_taxonomy


def modes(text, taxonomy):
    return sorted(h.mode_id for h in tag_text(text, taxonomy))


class TestTaxonomy:
    def test_size_and_ids(self, taxonomy):
        assert 55 <= len(taxonomy) <= 70
        assert all(m.definition and m.cfr for m in taxonomy)

    def test_seed_categories_present(self, taxonomy):
        assert {"SPR", "AUD", "ACC", "BKP", "EDC", "CLD", "P11", "DI"} <= {m.category for m in taxonomy}

    def test_rejects_unknown_related(self):
        raw = {"version": "x", "categories": {"AUD": "Audit"}, "modes": [
            {"id": "CS-AUD-001", "category": "AUD", "name": "n", "definition": "d", "related": ["CS-AUD-999"]}]}
        with pytest.raises(TaxonomyError):
            parse_taxonomy(raw)

    def test_rejects_duplicate(self):
        m = {"id": "CS-AUD-001", "category": "AUD", "name": "n", "definition": "d"}
        with pytest.raises(TaxonomyError):
            parse_taxonomy({"version": "x", "categories": {"AUD": "Audit"}, "modes": [m, m]})


class TestRules:
    @pytest.mark.parametrize("text,expected", [
        ("Audit trails for the HPLC system were not reviewed as part of batch release.", ["CS-AUD-002"]),
        ("The audit trail on the balance software was disabled.", ["CS-AUD-001"]),
        ("All analysts used a shared login to access the chromatography software.", ["CS-ACC-001"]),
        ("Analysts had administrator privileges allowing them to delete data.", ["CS-ACC-002"]),
        ("Results were calculated in an Excel spreadsheet that had not been validated.", ["CS-SPR-001"]),
        ("Restore of backup data has never been tested.", ["CS-BKP-003"]),
        ("The firm relies on its cloud provider, and backup restore was never tested.", ["CS-BKP-003", "CS-CLD-003"]),
    ])
    def test_true_findings(self, taxonomy, text, expected):
        assert modes(text, taxonomy) == expected

    @pytest.mark.parametrize("text", [
        "The reconstituted solution was cloudy after 24 hours.",          # word boundary: cloud
        "Files were stored on a shared drive.",                           # shared drive is not a shared login
        "Media fills were performed on shared equipment.",
    ])
    def test_word_boundary_traps(self, taxonomy, text):
        assert modes(text, taxonomy) == []

    @pytest.mark.parametrize("text", [
        "The audit trail was enabled and was adequately reviewed by QA.",  # adequate control
        "User access was appropriately restricted.",
        "We did not observe shared logins on the LIMS.",                   # negated finding
        "There was no evidence that audit trails were disabled.",
        "Your response should include an assessment of your audit trail review practices.",  # remediation ask
        "We recommend that you validate all spreadsheets used in GMP calculations.",
    ])
    def test_non_findings(self, taxonomy, text):
        assert modes(text, taxonomy) == []

    def test_adequate_with_but_is_still_finding(self, taxonomy):
        assert modes("Audit trails were enabled; however, they were not reviewed.", taxonomy) == ["CS-AUD-002"]

    def test_deficiency_language_is_not_treated_as_negation(self):
        # "not reviewed" is the finding, not a negation of it.
        assert sentence_is_finding("Audit trails were not reviewed.")

    def test_qmsr_vocabulary_maps_same(self, taxonomy):
        qsr = "Your firm failed to validate computer software for its intended use (21 CFR 820.70(i)). The software had not been validated."
        qmsr = ("Your firm failed to validate computer software as required by ISO 13485:2016 clause 4.1.6, "
                "incorporated by 21 CFR 820.10. The software had not been validated.")
        assert "CS-VAL-001" in modes(qsr, taxonomy)
        assert "CS-VAL-001" in modes(qmsr, taxonomy)
