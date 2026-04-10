from __future__ import annotations

from dataclasses import dataclass

from .payment_service import PaymentActorKind


@dataclass(frozen=True)
class ProducerFlow:
    test_confirmed_by: frozenset[PaymentActorKind]
    real_confirmed_by: frozenset[PaymentActorKind]
    thread_cleanup: bool = False
    notify_admin_dm_on_approval: bool = False


PRODUCER_FLOWS: dict[str, ProducerFlow] = {
    'grants': ProducerFlow(
        test_confirmed_by=frozenset({PaymentActorKind.AUTO}),
        real_confirmed_by=frozenset({PaymentActorKind.RECIPIENT_CLICK}),
    ),
    'admin_chat': ProducerFlow(
        test_confirmed_by=frozenset({PaymentActorKind.AUTO}),
        real_confirmed_by=frozenset(
            {
                PaymentActorKind.RECIPIENT_CLICK,
                PaymentActorKind.RECIPIENT_MESSAGE,
            }
        ),
    ),
}


def get_flow(producer: str) -> ProducerFlow:
    normalized_producer = str(producer).strip().lower()
    return PRODUCER_FLOWS[normalized_producer]


__all__ = ['PRODUCER_FLOWS', 'ProducerFlow', 'get_flow']
