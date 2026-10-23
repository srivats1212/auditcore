"""Which documents are in scope: GxP drugs, biologics and devices.

Food and veterinary letters also say "CGMP" (21 CFR 117, 507) and are out of
scope. The issuing office is the main signal, but it changed over time:
before FDA's October 2024 reorganization most domestic drug and device
letters came from ORA field divisions ("Division of Pharmaceutical Quality
Operations I", "Office of Medical Device and Radiological Health Division
II"), not from CDER or CDRH. Offices that cover several product areas
(OII, ORA, import divisions) fall back to the letter's subject line.
"""

from __future__ import annotations

import re

# Product-area offices: the centers and their field divisions.
_PRODUCT_OFFICE = re.compile(
    r"\b(CDER|CBER|CDRH|Drug Evaluation|Biologics|Biological Products|Devices? and Radiological|Medical Devices?"
    r"|Pharmaceutical Quality|Manufacturing Quality|Bioresearch Monitoring)\b", re.I)
_FOOD_VET_OFFICE = re.compile(
    r"\b(Foods?|CFSAN|Veterinary|Animal Food|Human and Animal Food|Tobacco|Nutrition)\b", re.I)
_PRODUCT_SUBJECT = re.compile(
    r"\b(Pharmaceuticals?|API|Active Pharmaceutical|Drugs?|OTC|Medical Devices?|QSR|QMSR|Biologics?|BLA|"
    r"Blood|Tissue|HCT/Ps?|Outsourcing Facility|Compounding|Positron Emission)\b", re.I)
_FOOD_VET_SUBJECT = re.compile(
    r"\b(Foods?|Seafood|HACCP|Dietary Supplements?|Juice|Infant Formula|Animals?|Feed|Shell Eggs?|Produce)\b", re.I)
# Food, labeling and radiation-control parts of 21 CFR.
_OUT_OF_SCOPE_PARTS = re.compile(r"^(1|10[0-9]|11[0-9]|50[0-9]|1[0-9]{3})$")


def letter_in_scope(office: str | None, subject: str | None) -> bool:
    """A warning letter about drugs, biologics or devices."""
    if office and _FOOD_VET_OFFICE.search(office) and not _PRODUCT_OFFICE.search(office):
        return False
    if office and _PRODUCT_OFFICE.search(office):
        return True
    subject = subject or ""
    return bool(_PRODUCT_SUBJECT.search(subject)) and not _FOOD_VET_SUBJECT.search(subject)


def in_scope(kind: str, office: str | None, subject: str | None, cfr_part: str | None) -> bool:
    """Citation-level scope for reports. Published 483s from the OII reading
    room are drug-only today. A letter with no office or subject on record
    falls back to the CFR part it cites."""
    if kind == "483":
        return True
    if office or subject:
        return letter_in_scope(office, subject)
    return bool(cfr_part) and not _OUT_OF_SCOPE_PARTS.match(cfr_part)
