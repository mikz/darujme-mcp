from __future__ import annotations

from typing import Any


def sample_money(cents: int = 12345, currency: str = "CZK") -> dict[str, Any]:
    return {"cents": cents, "currency": currency}


def sample_donor(**overrides: Any) -> dict[str, Any]:
    donor = {
        "firstName": "Jana",
        "lastName": "Novakova",
        "email": "jana@example.org",
        "address": {
            "street": "Otakarova 34",
            "city": "Praha",
            "postCode": "120 00",
            "country": "CR",
        },
        "phone": "+420 123 456 789",
        "companyName": "Firma s.r.o.",
        "companyIdentificationNumber": "45612378",
        "companyVatIdentificationNumber": "CZ45612378",
        "customFields": {"rodne_cislo": "secret"},
    }
    donor.update(overrides)
    return donor


def sample_pledge(**overrides: Any) -> dict[str, Any]:
    pledge = {
        "pledgeId": 1203450,
        "organizationId": 2,
        "projectId": 4563,
        "promotionId": None,
        "paymentMethod": "gp_webpay_charge",
        "isRecurrent": False,
        "recurrentState": "one_time",
        "pledgedAmount": sample_money(),
        "pledgedAt": "2026-05-01T10:00:00+02:00",
        "comment": "for the project",
        "lastModifiedDateTime": "2026-05-02T10:00:00+02:00",
        "donor": sample_donor(),
        "wantDonationCertificate": True,
        "customFields": {"source": "web"},
        "transactions": [],
    }
    pledge.update(overrides)
    return pledge


def sample_transaction(**overrides: Any) -> dict[str, Any]:
    transaction = {
        "transactionId": 7654321,
        "presentableCode": "2026000001",
        "state": "success",
        "sentAmount": sample_money(),
        "receivedAt": "2026-05-03T10:00:00+02:00",
        "outgoingAmount": sample_money(12000),
        "outgoingVs": "990001",
        "outgoingBankAccount": "123456789/0800",
        "lastModifiedDateTime": "2026-05-04T10:00:00+02:00",
        "pledge": sample_pledge(transactions=[]),
    }
    transaction.update(overrides)
    return transaction


def sample_project(**overrides: Any) -> dict[str, Any]:
    project = {
        "projectId": 4563,
        "promotionId": None,
        "organization": {"organizationId": 2, "name": "NNO", "logo": None},
        "collectedAmountEstimate": sample_money(500000),
        "donorsCount": 42,
        "title": {"cs": "Projekt"},
        "synopsis": {"cs": "Kratky popis"},
        "content": {"cs": "<p>Obsah</p>"},
        "targetAmount": sample_money(1000000),
        "activeUntil": None,
        "donateUrl": "https://www.darujme.cz/projekt/4563",
        "tags": [],
    }
    project.update(overrides)
    return project


def sample_promotion(**overrides: Any) -> dict[str, Any]:
    promotion = {
        **sample_project(),
        "projectId": 4563,
        "promotionId": 9876,
        "title": {"cs": "Vyzva"},
        "donateUrl": "https://www.darujme.cz/vyzva/9876",
    }
    promotion.update(overrides)
    return promotion
