"""Implements: FA73 (Ausgabelizenz und Lizenzkompatibilität je Ausgabe).

Prüft je deklarierter Ausgabe, ob die Lizenzen ihrer Quellen (Vereinigung aus
``ExecutionPlan.attribution``, FA65) zusammen mit der Ausgabelizenz
(``output[i].license`` bzw. ``scenario.output_license``) laut DALICC vereinbar
sind. Die Prüfung ist netzgebunden und läuft nur ausdrücklich
(``geofact licenses``, ``geofact validate --check-licenses``,
``geofact.api.check_licenses``, Web ``POST /api/config/licenses``); ein Lauf
liest nur die Ablage (``output_license_for``: Prüfvermerk "geprüft am ...").

Ergebnis je Ausgabe (``LicenseCheck.status``):

- ``compatible``     alle Lizenzen zugeordnet, in DALICC vorhanden, keine Konflikte
- ``conflicts``      DALICC meldet Konflikte (Grund, beide Aussagen, deutsche Meldung)
- ``not_checkable``  mindestens eine Lizenz ohne DALICC-Kennung bzw. unbekannt in
                     DALICC und unter den übrigen kein Konflikt - nie "vereinbar"
- ``unreachable``    DALICC nicht erreichbar oder Antwort unbrauchbar
- ``no_licenses``    weder Quell- noch Ausgabelizenz

Das Ergebnis ist keine Rechtsauskunft: DALICC vergleicht ODRL-Aussagen der
Lizenztexte; die Ausgabelizenz bleibt eine Festlegung des Nutzers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from geofact.core.contracts import License
from geofact.core.plan import ExecutionPlan
from geofact.core.provenance import NodeAttribution, OutputLicense
from geofact.core.scenario import OutputSpec, Scenario
from geofact.support import dalicc
from geofact.support.dalicc import DaliccClient, DaliccUnavailable

Status = Literal[
    "compatible", "conflicts", "not_checkable", "unreachable", "no_licenses"
]

_STATUS_TEXT: dict[str, str] = {
    "compatible": "vereinbar",
    "conflicts": "Konflikt",
    "not_checkable": "nicht prüfbar",
    "unreachable": "Dienst nicht erreichbar",
    "no_licenses": "keine Lizenzen",
}

_DEONTIC = {
    "permission": "erlaubt",
    "prohibition": "verbietet",
    "duty": "verlangt",
    "obligation": "verlangt",
}


@dataclass(frozen=True)
class LicenseConflict:
    """Ein von DALICC gemeldeter Konflikt zweier Aussagen (FA73).

    ``kind``: ``direct`` oder ``derived``; ``statement_1``/``statement_2``:
    (Lizenz-URI, ODRL-Regel-URI, Handlungs-URI) wie von der API geliefert."""

    kind: str
    reason: str
    statement_1: tuple[str, ...]
    statement_2: tuple[str, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "statement_1": list(self.statement_1),
            "statement_2": list(self.statement_2),
            "message": self.message,
        }


@dataclass(frozen=True)
class UncheckableLicense:
    """Eine Lizenz, die DALICC nicht prüfen kann, mit Grund."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class LicenseCheck:
    """Kompatibilität der Lizenzen EINER Ausgabe (FA73)."""

    index: int
    source: str
    type: str
    output_license: Optional[License]
    declared_at: str
    source_licenses: tuple[License, ...]
    undeclared: tuple[str, ...]
    status: Status
    message: str
    conflicts: tuple[LicenseConflict, ...] = ()
    uncheckable: tuple[UncheckableLicense, ...] = ()
    checked_at: Optional[str] = None

    def note(self) -> Optional[str]:
        """Prüfvermerk für die Ausgabe: ``geprüft am 2026-10-03: vereinbar
        (DALICC)``; None, wenn nicht geprüft (kein Zeitpunkt)."""
        if self.checked_at is None:
            return None
        return (
            f"geprüft am {self.checked_at[:10]}: {_STATUS_TEXT[self.status]} (DALICC)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "source": self.source,
            "type": self.type,
            "output_license": (
                self.output_license.to_dict()
                if self.output_license is not None
                else None
            ),
            "declared_at": self.declared_at or None,
            "source_licenses": [lic.to_dict() for lic in self.source_licenses],
            "undeclared": list(self.undeclared),
            "status": self.status,
            "message": self.message,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "uncheckable": [item.to_dict() for item in self.uncheckable],
            "checked_at": self.checked_at,
        }


@dataclass
class LicenseCheckReport:
    """Ergebnis der Prüfung aller Ausgaben; ``status`` ist der schlechteste
    Einzelstatus (unreachable > conflicts > not_checkable > compatible)."""

    checks: list[LicenseCheck] = field(default_factory=list)

    @property
    def status(self) -> Status:
        for candidate in ("unreachable", "conflicts", "not_checkable", "compatible"):
            if any(check.status == candidate for check in self.checks):
                return candidate  # type: ignore[return-value]
        return "no_licenses"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


def _tail(uri: str) -> str:
    """Letzter Teil einer URI (``.../ns#chargeDistributionFee`` -> ``chargeDistributionFee``)."""
    for separator in ("#", "/"):
        if separator in uri.rstrip(separator):
            uri = uri.rstrip(separator).rsplit(separator, 1)[1]
    return uri


def _statement_text(statement: tuple[str, ...], names: dict[str, str]) -> str:
    if len(statement) < 3:
        return " ".join(statement)
    license_uri, rule, action = statement[0], statement[1], statement[2]
    verb = _DEONTIC.get(_tail(rule).casefold(), _tail(rule))
    return f"{names.get(license_uri, _tail(license_uri))} {verb} „{_tail(action)}“"


def _conflicts(
    response: dict[str, Any], names: dict[str, str]
) -> list[LicenseConflict]:
    statements = response.get("conflicting_statements") or {}
    found: list[LicenseConflict] = []
    for kind in ("direct", "derived"):
        entries = statements.get(kind) or {}
        for key in sorted(entries, key=lambda k: (len(str(k)), str(k))):
            entry = entries[key] or {}
            first = tuple(str(part) for part in entry.get("statement_1") or ())
            second = tuple(str(part) for part in entry.get("statement_2") or ())
            reason = str(entry.get("reason") or "")
            label = "Direkter" if kind == "direct" else "Abgeleiteter"
            message = (
                f"{label} Konflikt: {_statement_text(first, names)}, "
                f"{_statement_text(second, names)}"
                + (f" (DALICC: {reason})" if reason else "")
            )
            found.append(
                LicenseConflict(
                    kind=kind,
                    reason=reason,
                    statement_1=first,
                    statement_2=second,
                    message=message,
                )
            )
    return found


def _licenses_of(
    attribution: Optional[NodeAttribution],
) -> tuple[tuple[License, ...], tuple[str, ...]]:
    if attribution is None:
        return (), ()
    return attribution.licenses, attribution.undeclared


def check_output(
    scenario: Scenario,
    plan: ExecutionPlan,
    index: int,
    client: DaliccClient,
) -> Optional[LicenseCheck]:
    """Prüfung einer Ausgabe. Offline (``client.offline``) ohne Ablage: None.
    ``DaliccUnavailable`` wird zum Status ``unreachable``."""
    spec: OutputSpec = scenario.output[index]
    output_license, declared_at = scenario.output_license_of(spec)
    source_licenses, undeclared = _licenses_of(plan.attribution.get(spec.source))
    involved: list[License] = list(source_licenses)
    if output_license is not None and output_license not in involved:
        involved.append(output_license)

    def result(status: Status, message: str, **extra: Any) -> LicenseCheck:
        return LicenseCheck(
            index=index,
            source=spec.source,
            type=spec.type,
            output_license=output_license,
            declared_at=declared_at,
            source_licenses=tuple(source_licenses),
            undeclared=tuple(undeclared),
            status=status,
            message=message,
            **extra,
        )

    if not involved:
        return result(
            "no_licenses", "Weder die Quellen noch die Ausgabe tragen eine Lizenz."
        )

    uncheckable: list[UncheckableLicense] = []
    names: dict[str, str] = {}
    for license_ in involved:
        resolution = dalicc.resolve(license_.name, license_.id)
        if resolution.uri is None:
            uncheckable.append(
                UncheckableLicense(license_.name, resolution.reason or "")
            )
        else:
            names.setdefault(resolution.uri, license_.name)

    try:
        library = client.library() if names else {}
        if library is None:
            return None
        for uri, name in list(names.items()):
            if uri not in library:
                uncheckable.append(
                    UncheckableLicense(
                        name,
                        f"Kennung {uri} ist in der DALICC-Bibliothek nicht vorhanden "
                        "(eine unbekannte Kennung meldete die API sonst als konfliktfrei)",
                    )
                )
                del names[uri]
        record = client.check(names) if names else None
        if names and record is None:
            return None
    except DaliccUnavailable as exc:
        return result(
            "unreachable",
            f"DALICC nicht erreichbar - Lizenzen nicht geprüft: {exc}",
            uncheckable=tuple(uncheckable),
        )

    # Zeitpunkt nur aus der Prüfung genau dieser Kennungsmenge, nie aus der
    # Lizenzliste, die eine fremde Prüfung abgelegt haben kann.
    checked_at = record["checked_at"] if record is not None else None
    conflicts = _conflicts(record["response"], names) if record is not None else []
    uncheckable_text = "; ".join(f"{item.name}: {item.reason}" for item in uncheckable)
    undeclared_text = (
        f"Quelle(n) ohne deklarierte Lizenz: {', '.join(undeclared)} - "
        "deren Bedingungen sind unbekannt"
        if undeclared
        else ""
    )
    if conflicts:
        message = f"{len(conflicts)} Konflikt(e) laut DALICC: " + " · ".join(
            conflict.message for conflict in conflicts
        )
        if uncheckable:
            message += f" Nicht prüfbar: {uncheckable_text}."
        if undeclared:
            message += f" {undeclared_text}."
        status: Status = "conflicts"
    elif uncheckable or undeclared:
        # Eine Quelle ohne Lizenz ist nie "vereinbar" (Regel 7).
        status = "not_checkable"
        reasons = "; ".join(
            text for text in (uncheckable_text, undeclared_text) if text
        )
        message = f"Nicht prüfbar (kein Urteil über die Vereinbarkeit): {reasons}."
        if len(names) >= 2:
            message += f" Unter den übrigen ({', '.join(sorted(names.values()))}) kein Konflikt."
    else:
        status = "compatible"
        message = (
            f"Laut DALICC vereinbar: {', '.join(names[uri] for uri in sorted(names))}."
            if len(names) >= 2
            else f"Nur eine Lizenz ({next(iter(names.values()))}) - vereinbar."
        )
    return result(
        status,
        message,
        conflicts=tuple(conflicts),
        uncheckable=tuple(uncheckable),
        checked_at=checked_at,
    )


def check_licenses(
    scenario: Scenario,
    plan: ExecutionPlan,
    *,
    refresh: bool = False,
    client: Optional[DaliccClient] = None,
) -> LicenseCheckReport:
    """FA73: Kompatibilitätsprüfung aller Ausgaben über DALICC (netzgebunden,
    Antworten unter ``<GEOFACT_SNAPSHOT_DIR>/dalicc/``). ``refresh`` fragt neu."""
    client = client if client is not None else DaliccClient(refresh=refresh)
    report = LicenseCheckReport()
    for index in range(len(scenario.output)):
        check = check_output(scenario, plan, index, client)
        if check is not None:
            report.checks.append(check)
    return report


def output_license_for(
    scenario: Scenario,
    plan: Optional[ExecutionPlan],
    spec: OutputSpec,
    client: Optional[DaliccClient] = None,
) -> Optional[OutputLicense]:
    """Wirksame Ausgabelizenz mit Prüfvermerk aus der Ablage (nie Netz): "geprüft
    am ...", wenn eine frühere Prüfung dieser Lizenzmenge vorliegt, sonst
    ungeprüft. None ohne Ausgabelizenz."""
    license_, declared_at = scenario.output_license_of(spec)
    if license_ is None:
        return None
    note: Optional[str] = None
    if plan is not None:
        index = next(i for i, item in enumerate(scenario.output) if item is spec)
        offline = client if client is not None else DaliccClient(offline=True)
        try:
            check = check_output(scenario, plan, index, offline)
        except Exception:  # noqa: BLE001 - ein Ablagefehler macht die Ausgabe nur "ungeprüft"
            check = None
        if check is not None and check.status != "unreachable":
            note = check.note()
    return OutputLicense(
        license=license_,
        declared_at=declared_at,
        check=note,  # type: ignore[arg-type]
    )
