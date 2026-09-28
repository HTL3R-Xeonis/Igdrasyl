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
                "passwords": ["junioradmin", "REMOVED"],
            }
        ]
    }
    with patch("app.guest_ops.APIHandler.get", return_value=templates):
        assert guest_credentials_for_image("debian") == ("root", "REMOVED")


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
