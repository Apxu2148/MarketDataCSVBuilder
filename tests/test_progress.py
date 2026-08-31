from market_data_csv_builder.progress import StageProgress


def test_progress_includes_source_stage_counts_elapsed_and_flushes(capsys) -> None:
    progress = StageProgress("moex", "liquidity scan", 10)
    progress.start()
    progress.update(3, force=True, passed=2, failed=1)
    progress.warning("TEST", "synthetic failure\nwith details")
    progress.complete(processed=10, passed=8, failed=2)
    output = capsys.readouterr().out
    assert "MOEX liquidity scan started: 0/10" in output
    assert "MOEX liquidity scan: 3/10" in output
    assert "passed=2" in output and "failed=1" in output and "elapsed=" in output
    assert "WARNING MOEX liquidity scan TEST: synthetic failure with details" in output
    assert "MOEX liquidity scan completed" in output
