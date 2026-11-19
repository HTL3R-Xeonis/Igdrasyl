from unittest.mock import MagicMock, patch

from app.guest_ops import guest_credentials_for_image, run_command_in_guest


def test_guest_credentials_for_image_prefers_root():
    """
    Real bug found live: the first (index-0) user/password pair was
    always used, but on every Linux template that's the non-root
    junioradmin account, which has no passwordless sudo configured -
    config-push commands needing root (nearly all of them) failed
    silently. root's own credentials are right there in the same
    response, just at a later index.
    """
    templates = {
        "templates": [
            {
                "name": "debian",
                "users": ["junioradmin", "root"],
                "passwords": ["junioradmin", "lab-root-pw"],
            }
        ]
    }
    with patch("app.guest_ops.APIHandler.get", return_value=templates):
        assert guest_credentials_for_image("debian") == ("root", "lab-root-pw")


def test_guest_credentials_for_image_falls_back_without_root():
    templates = {
        "templates": [
            {
                "name": "Windows 10 Pro Education",
                "users": ["junioradmin"],
                "passwords": ["junioradmin"],
            }
        ]
    }
    with patch("app.guest_ops.APIHandler.get", return_value=templates):
        assert guest_credentials_for_image("Windows 10 Pro Education") == (
            "junioradmin",
            "junioradmin",
        )


def test_run_command_in_guest_returns_none_on_method_fault():
    """
    vmodl.MethodFault is the real base fault class - vim.fault.MethodFault
    (what the code used to catch) doesn't exist at all; see
    test_run_command_in_guest_vim_fault_methodfault_does_not_exist.
    """
    from pyVmomi import vmodl

    process_manager = MagicMock()
    process_manager.StartProgramInGuest.side_effect = vmodl.MethodFault()

    exit_code, _output = run_command_in_guest(
        process_manager, MagicMock(), MagicMock(), "echo hi", is_windows=False
    )

    assert exit_code is None


def test_run_command_in_guest_vim_fault_methodfault_does_not_exist():
    """
    Documents the real bug this project hit live: `vim.fault.MethodFault`
    isn't a real attribute in pyvmomi's lazily-loaded WSDL type system
    (only `vmodl.MethodFault`, the actual base class, exists) - merely
    referencing it raises AttributeError('MethodFault'), which is
    exactly the cryptic error this project's own logs showed for a VM
    with no VMware Tools installed, before this was understood.
    """
    from pyVmomi import vim

    assert not hasattr(vim.fault, "MethodFault")


def test_run_command_in_guest_returns_none_on_unexpected_exception():
    """
    Real bug found live: a guest with no VMware Tools installed (e.g.
    OPNsense) doesn't fail StartProgramInGuest with a clean
    vim.fault.MethodFault - pyvmomi's own SOAP-fault deserialization
    raised a raw AttributeError instead, which used to propagate
    straight past the narrower except clause and break this function's
    own documented "always returns (None, reason), never raises"
    contract.
    """
    process_manager = MagicMock()
    process_manager.StartProgramInGuest.side_effect = AttributeError("MethodFault")

    exit_code, output = run_command_in_guest(
        process_manager, MagicMock(), MagicMock(), "echo hi", is_windows=False
    )

    assert exit_code is None
    assert "AttributeError" in output


def test_run_command_in_guest_returns_exit_code_on_success():
    process_manager = MagicMock()
    process_manager.StartProgramInGuest.return_value = 123
    process_manager.ListProcessesInGuest.return_value = [
        MagicMock(endTime="2026-01-01T00:00:00Z", exitCode=0)
    ]

    exit_code, _output = run_command_in_guest(
        process_manager, MagicMock(), MagicMock(), "echo hi", is_windows=False
    )

    assert exit_code == 0


def _guest_mocks(file_text="hello\n", exit_code=0):
    process_manager = MagicMock()
    process_manager.StartProgramInGuest.return_value = 7
    process_manager.ListProcessesInGuest.return_value = [
        MagicMock(endTime="2026-01-01T00:00:00Z", exitCode=exit_code)
    ]
    file_manager = MagicMock()
    file_manager.InitiateFileTransferFromGuest.return_value = MagicMock(
        url="https://*/guestFile?id=1&token=t"
    )
    response = MagicMock()
    response.content = file_text.encode()
    return process_manager, file_manager, response


def test_output_is_captured_through_a_guest_file_and_removed_again():
    process_manager, file_manager, response = _guest_mocks("web01\n")
    with patch("requests.get", return_value=response) as get:
        exit_code, output = run_command_in_guest(
            process_manager,
            MagicMock(),
            MagicMock(),
            "hostname",
            is_windows=False,
            file_manager=file_manager,
            esxi_host="10.20.20.202",
        )
    assert (exit_code, output) == (0, "web01\n")
    assert get.call_args.args[0].startswith("https://10.20.20.202/guestFile")
    file_manager.DeleteFileInGuest.assert_called_once()
    spec = process_manager.StartProgramInGuest.call_args.args[2]
    assert spec.programPath == "/bin/bash"
    assert "> /tmp/topo-push-" in spec.arguments


def test_a_command_with_quotes_and_variables_reaches_bash_intact():
    process_manager, file_manager, response = _guest_mocks()
    command = "echo \"a b\" 'c' $HOME | tr a-z A-Z"
    with patch("requests.get", return_value=response):
        run_command_in_guest(
            process_manager,
            MagicMock(),
            MagicMock(),
            command,
            is_windows=False,
            file_manager=file_manager,
            esxi_host="h",
        )
    arguments = process_manager.StartProgramInGuest.call_args.args[2].arguments
    # one single-quoted bash -c argument; an inner ' is closed, escaped and reopened
    assert arguments.startswith("-c '{ echo \"a b\" '\\''c'\\'' $HOME | tr a-z A-Z")


def test_windows_commands_are_grouped_before_redirecting():
    process_manager, file_manager, response = _guest_mocks()
    with patch("requests.get", return_value=response):
        run_command_in_guest(
            process_manager,
            MagicMock(),
            MagicMock(),
            "ipconfig && hostname",
            is_windows=True,
            file_manager=file_manager,
            esxi_host="h",
        )
    spec = process_manager.StartProgramInGuest.call_args.args[2]
    assert spec.programPath.endswith("cmd.exe")
    assert spec.arguments.startswith("/c (ipconfig && hostname) > ")


def test_when_the_output_cannot_be_fetched_the_exit_code_is_still_reported():
    process_manager, file_manager, _ = _guest_mocks(exit_code=3)
    file_manager.InitiateFileTransferFromGuest.side_effect = RuntimeError("no tools")
    exit_code, output = run_command_in_guest(
        process_manager,
        MagicMock(),
        MagicMock(),
        "false",
        is_windows=False,
        file_manager=file_manager,
        esxi_host="h",
    )
    assert (exit_code, output) == (3, "exit code 3")
    file_manager.DeleteFileInGuest.assert_called_once()


def test_an_empty_output_falls_back_to_the_exit_code():
    process_manager, file_manager, response = _guest_mocks("")
    with patch("requests.get", return_value=response):
        _code, output = run_command_in_guest(
            process_manager,
            MagicMock(),
            MagicMock(),
            "true",
            is_windows=False,
            file_manager=file_manager,
            esxi_host="h",
        )
    assert output == "exit code 0"
