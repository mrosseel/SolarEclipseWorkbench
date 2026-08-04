"""XSDK_SetProp and XSDK_GetProp take an api_param, and it is not free-form.

Every call site in the wrapper passed 0 by hand.  The SDK's own headers list
one required number per API per model, and 25 of our 28 calls disagreed with
the X-T4's, so the body answered 0x1002 "Invalid parameter" - which is why the
focus mode was unreadable and the live view size unsettable.
"""

import ctypes

from fujixsdk import _constants as C
from fujixsdk.camera import _resolve_param


def test_the_live_view_numbers_match_the_vendor_sample():
    # SAMPLES/Windows/LiveView/live_view_control.py passes 1 for the size and
    # the quality and 0 for the start, which is the whole bug in three lines.
    assert C.api_param(C.API_CODE_SetLiveViewImageSize) == 1
    assert C.api_param(C.API_CODE_SetLiveViewImageQuality) == 1
    assert C.api_param(C.API_CODE_StartLiveView) == 0
    assert C.api_param(C.API_CODE_StopLiveView) == 0


def test_the_apis_that_failed_on_the_bench_are_no_longer_zero():
    # These are the two the log named, 4 August.
    assert C.api_param(C.API_CODE_GetFocusMode) == 1
    assert C.api_param(C.API_CODE_GetImageQuality) == 1


def test_an_unknown_api_falls_back_to_zero():
    # Where the wrapper already was, so an API missing from the table is no
    # worse off than before.
    assert C.api_param(0xDEAD) == 0


def test_an_omitted_param_is_looked_up():
    value = ctypes.c_long(1)
    param, args = _resolve_param(C.API_CODE_SetLiveViewImageSize, None, (value,))

    assert param == 1
    assert args == (value,)


def test_a_value_passed_where_the_param_used_to_go_is_not_mistaken_for_one():
    # The param was positional and mandatory, so an old-style caller's first
    # variadic argument lands in its slot.  Treating a ctypes value as a
    # parameter number would send garbage to the body.
    value = ctypes.c_long(3)
    param, args = _resolve_param(C.API_CODE_SetLiveViewImageSize, value, ())

    assert param == 1
    assert args == (value,)


def test_an_explicit_param_still_wins():
    param, args = _resolve_param(C.API_CODE_SetLiveViewImageSize, 7, ())

    assert param == 7
    assert args == ()


def test_the_live_view_sizes_are_the_sizes_the_body_makes():
    # Named XGA/VGA/QVGA with resolutions to match, none of which the body
    # produces.  The numbers were always right; only the description was not.
    assert C.LIVEVIEW_SIZE_L == 0x0001
    assert C.LIVEVIEW_SIZE_XGA == C.LIVEVIEW_SIZE_L      # old name still works
    assert C.LIVEVIEW_QUALITY_BASIC == 0x0003            # was missing entirely
