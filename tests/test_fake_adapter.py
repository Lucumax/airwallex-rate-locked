from rate_locked.awx.adapter import AirwallexAdapter
from rate_locked.awx.fake import FakeAirwallex

from adapter_contract import AdapterContract


class TestFakeAdapter(AdapterContract):
    def make(self):
        fake = FakeAirwallex.demo()
        return fake, fake


def test_fake_satisfies_the_adapter_protocol():
    assert isinstance(FakeAirwallex.demo(), AirwallexAdapter)


def test_strict_transitions_follow_the_reference_lifecycle():
    from decimal import Decimal as D
    import pytest
    from rate_locked.awx.adapter import AirwallexError
    from rate_locked.awx.fake import FakeAirwallex
    f = FakeAirwallex.demo(strict_transitions=True)
    ben = f.create_beneficiary(request_id="b", payload={"name": "X"})
    t = f.create_transfer(request_id="t", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                          transfer_currency="EUR", transfer_amount=D("5"), reason="r", reference="x")
    with pytest.raises(AirwallexError):
        f.simulate_transfer_transition(t.transfer_id, "PAID")  # PROCESSING cannot jump to PAID
    assert f.simulate_transfer_transition(t.transfer_id, "SENT").status == "SENT"
    assert f.simulate_transfer_transition(t.transfer_id, "PAID").status == "PAID"
    assert f.simulate_transfer_transition(t.transfer_id, "FAILED").status == "FAILED"  # PAID can still fail late
