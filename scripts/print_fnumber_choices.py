#!/usr/bin/env python3
"""Print the available f-number choices reported by the connected camera via gphoto2.

Run this script with the camera connected and switched on (in PC Remote / MTP mode).
It will show exactly what strings gphoto2 exposes for the f-number widget, which is
the information needed to diagnose aperture-setting failures.
"""

import sys

try:
    import gphoto2 as gp
except ImportError:
    sys.exit("gphoto2 Python bindings not found.  Install them with:  pip install gphoto2")

context = gp.Context()
camera = gp.Camera()

print("Connecting to camera …")
try:
    camera.init(context)
except gp.GPhoto2Error as e:
    sys.exit(f"Could not connect to camera: {e}\n"
             "Make sure the camera is on, connected via USB, and in PC Remote mode.")

try:
    config = camera.get_config(context)

    # Try the 'f-number' widget name (Nikon/Sony via PTP)
    widget_names = ['f-number', 'aperture']
    f_widget = None
    used_name = None
    for name in widget_names:
        try:
            f_widget = config.get_child_by_name(name)
            used_name = name
            break
        except gp.GPhoto2Error:
            pass

    if f_widget is None:
        print("Neither 'f-number' nor 'aperture' widget found on this camera.")
        print("Available top-level config children:")
        for child in config.get_children():
            print(f"  {child.get_name()}")
    else:
        current = f_widget.get_value()
        print(f"Widget name : '{used_name}'")
        print(f"Current value: {current!r}")
        print()
        try:
            n = f_widget.count_choices()
            print(f"{n} available choice(s):")
            for i in range(n):
                choice = f_widget.get_choice(i)
                marker = "  <-- current" if choice == current else ""
                print(f"  [{i:3d}]  {choice!r}{marker}")
        except gp.GPhoto2Error:
            print("(Widget has no enumerated choices — it may accept free-form values.)")
finally:
    camera.exit(context)
    print("\nDone.")
