"""How many Postgres connections each process is allowed to hold.

Left to SQLAlchemy's defaults both processes got the same pool — keep five,
borrow ten more — and for the agent that was the wrong shape rather than merely
an untuned one: it runs two threads per account plus an indexer, each holding a
connection for a whole pass, so on any install past two accounts the steady
state sat permanently in overflow and every pass opened and closed real Postgres
backends. The numbers below are the arithmetic that replaces that, and they are
pinned here because nothing else notices when they drift: an undersized pool
does not fail, it just churns.

Pure unit test — `pool_shape` takes the counts rather than reading them, so no
engine is built and no database is touched.
"""

import pytest

from core.database import pool_shape


def test_the_server_keeps_few_and_bursts_wide():
    # A request hands its connection straight back, and a burst of browsers
    # wants more at once than any process should hold between bursts.
    assert pool_shape(is_agent=False, accounts=5) == (5, 10)


def test_the_server_does_not_grow_with_the_account_count():
    # It runs no per-account threads; the accounts belong to the agent.
    assert pool_shape(is_agent=False, accounts=0) == pool_shape(is_agent=False, accounts=40)


@pytest.mark.parametrize("accounts,kept", [(2, 6), (3, 8), (5, 12), (10, 22)])
def test_the_agent_holds_two_connections_per_account_plus_the_indexer(accounts, kept):
    assert pool_shape(is_agent=True, accounts=accounts)[0] == kept


def test_the_agent_never_asks_for_less_than_the_floor():
    # A one-shot run against a config with no accounts in it still needs a pool.
    assert pool_shape(is_agent=True, accounts=0)[0] == 5
    assert pool_shape(is_agent=True, accounts=1)[0] == 5


def test_the_agents_steady_state_fits_inside_what_it_keeps():
    # The point of the whole change: at rest the agent must never reach for
    # overflow, or it opens and closes a backend on every pass.
    for accounts in range(1, 12):
        kept, _ = pool_shape(is_agent=True, accounts=accounts)
        threads = 2 * accounts + 1   # sync + lease keeper per account, one indexer
        assert kept >= threads


def test_an_explicit_budget_wins_over_both():
    assert pool_shape(is_agent=True, accounts=5, pool_size=3, max_overflow=1) == (3, 1)
    assert pool_shape(is_agent=False, accounts=5, pool_size=3, max_overflow=1) == (3, 1)


def test_no_burst_at_all_is_a_setting_someone_can_choose():
    # Which is why max_overflow's "decide it yourself" sentinel is -1: zero has
    # to keep meaning zero, or a database held to a hard connection budget
    # silently gets ten more than it was promised.
    assert pool_shape(is_agent=True, accounts=5, max_overflow=0)[1] == 0
    assert pool_shape(is_agent=True, accounts=5, max_overflow=-1)[1] == 4
