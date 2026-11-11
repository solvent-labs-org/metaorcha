# What MetaBot, the app, claims, and what proves each claim

Public copy about the app says only what a row here says, and every row names the test
that would fail if the claim stopped being true. `tests/test_claims.py` reads this table
and checks that each proof exists. A claim with no row is not made. Proof kinds:
`tests/<file>.py::<test>` (a unit test), `selftest:<journey>/<step>` (a self-test journey
step, run on Linux in CI and on the tablet), `tablet-e2e:<step>` (the scripted tablet
walk, `tools/tablet-e2e.sh`), `ui:<file>` (a page suite), `desktop:<test>` (a test of the desktop shell's Rust core). Several
proofs are separated by `;`.

| Claim | Proof |
|---|---|
| Every run ends in a signed receipt, and the app and the core both verify it on this machine | `selftest:ask_receipt/verify (app)`; `selftest:ask_receipt/verify (core)` |
| A run whose model server fails, cuts off, refuses or stalls still ends, says why, and leaves a receipt that verifies | `selftest:recovery_engine/error_500`; `selftest:recovery_engine/truncate`; `selftest:recovery_engine/refuse`; `selftest:recovery_engine/slow`; `selftest:recovery_engine/app run on a failing engine` |
| A receipt names every tool the run used, and the files it wrote are where it says | `selftest:tools_offline/receipt names the tools`; `selftest:tools_offline/files exist` |
| A workflow runs across more than one provider and more than one account, and its receipts name each engine, endpoint and ladder rung | `selftest:workflow_chain/receipts name every engine, account and rung` |
| A workflow's approval is given at a terminal; a script cannot give it | `selftest:workflow_chain/approve at a terminal` |
| An exported bundle verifies offline as one chain: the workflow file, its approval and every member receipt | `selftest:workflow_chain/bundle verifies offline` |
| A receipt that has been edited does not verify | `selftest:workflow_chain/a rewritten rung is caught` |
| A run stopped by a missing account still leaves receipts that verify | `selftest:workflow_chain/a missing account stops the chain, sealed` |
| A phone pairs by a typed code or a QR, pins the computer's certificate, and shows four words the computer also shows | `tablet-e2e:words match`; `tests/test_pairing.py::test_like_the_apk`; `tests/test_tlscert.py::test_the_handshake_presents_the_pinned_fingerprint` |
| A pairing code is used once, dies after five wrong tries, and expires | `tests/test_pair.py::test_start_then_claim_then_use`; `tests/test_pairing.py::test_five_wrong_tries_kill_the_code`; `tests/test_pairing.py::test_malformed_and_expired` |
| Revoking a phone closes that phone's live connections at once, and no other's | `tests/test_revoke.py::test_revoke_cuts_the_stream_and_the_terminal_of_that_device_only`; `tablet-e2e:pairing screen after revoke` |
| The desktop token works on loopback only; a phone token works on the TLS listener only | `tests/test_pair.py::test_scopes_of_the_routes`; `tests/test_devices.py::test_match_is_scope_bound` |
| A revoked phone's retries never lock the desktop out, and never block a new pairing | `tests/test_revoke.py::test_a_revoked_phone_never_locks_out_the_desktop_on_the_same_address`; `tests/test_revoke.py::test_a_stale_token_never_blocks_a_new_pairing_from_the_same_phone` |
| Device tokens are stored as hashes; a listing never shows one | `tests/test_devices.py::test_rows_persist_and_list_hides_hashes` |
| The page never shows a surface the host cannot serve: no desks without tmux, only the engines the host has | `tests/test_caps.py::test_no_tmux_means_no_desks`; `tests/test_caps.py::test_engines_follow_the_binaries` |
| The server starts once per state folder; a second start reports the first one's port and takes nothing over | `tests/test_instance.py::test_two_serves_give_one_server`; `tests/test_instance.py::test_a_port_taken_by_another_program_is_left_alone` |
| A model server on this machine is enough: no vendor login is needed for a first run and its receipt | `selftest:ask_receipt/run done` |
| The app keeps running across logins when asked, without root, and can be removed as easily | `tests/test_service.py::test_install_writes_the_unit_and_enables_it`; `tests/test_service.py::test_remove_disables_then_deletes_and_is_quiet_when_absent` |
| A run needs no tmux: on a host without it, and on the phone, the app runs the core as its own child, with the prompt on stdin, a log it can show, and one kill for the whole tree | `tests/test_run_process.py::test_no_tmux_means_a_process_run`; `tests/test_run_process.py::test_a_finished_run_reports_its_exit_and_its_receipt`; `tests/test_run_process.py::test_kill_ends_the_whole_process_tree` |
| Several runs go at once, the cap holds, and each run's receipt verifies | `selftest:concurrency/the runs overlapped`; `selftest:concurrency/a fourth is refused at the cap`; `selftest:concurrency/every receipt verifies` |
| A run killed with the app is listed as interrupted, never lost or shown as done, and the next run works | `tests/test_runs.py::test_a_run_whose_process_died_is_listed_as_interrupted`; `selftest:recovery_kill/the interrupted run is listed as such`; `selftest:recovery_kill/the next run works` |
| A wrong key fails fast, leaves a receipt that verifies, and the app says where to fix the key | `tests/test_run_report.py::test_a_refused_key_says_to_check_the_key`; `selftest:wrong_key/the run fails fast`; `selftest:wrong_key/the app says to check the key` |
| What a run did is readable and checkable: every step row names its tool, time and verdict; the app verifies the receipt; a tampered copy is reported invalid | `selftest:receipt_view_clarity/every step row shows tool, time and verdict`; `selftest:receipt_view_clarity/the verify route says valid`; `selftest:receipt_view_clarity/a tampered copy is reported invalid` |
| Nothing private ships in the tree: no names, no home paths, no keys | `tests/test_private_strings.py::test_no_private_string_in_the_shipped_tree` |
| The desktop window opens the server already running, or starts one from PATH and waits for it, and never prints the token | `desktop:waiting_for_a_server_returns_its_port_and_token_once_both_exist_and_gives_up_on_time`; `desktop:the_state_folder_is_found_the_way_the_server_finds_it`; `tests/test_desktop_shell.py::test_the_smoke_mode_prints_the_port_and_never_the_token` |
