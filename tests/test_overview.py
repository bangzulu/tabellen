from fp.overview import is_acute, rail_status

H = 3600


def test_acute_vs_planned():
    now = 1_000_000
    assert is_acute(now - H, now + 2 * H, now)              # 09:45–12:00: akut
    assert not is_acute(now - H, now + 3 * 24 * H, now)     # flera dagar: planerat
    assert is_acute(now - 2 * H, None, now)                 # utan slut, nyss publicerat
    assert not is_acute(now - 30 * H, None, now)


def test_rail_status():
    assert rail_status(30, 30, 1, 0, 4, 0, 0) == "normal"
    assert rail_status(30, 30, 1, 0, 4, 0, 1) == "minor"    # akut störning
    assert rail_status(34, 33, 17, 14, 10, 1, 1) == "major" # 40 % minst 5 min sena
    assert rail_status(2, 4, 0, 0, 2, 0, 0) == "minor"      # få tåg: färre än planerat ger bara minor
    assert rail_status(20, 30, 0, 0, 2, 0, 0) == "minor"    # saknad realtid räcker inte för "stora"
