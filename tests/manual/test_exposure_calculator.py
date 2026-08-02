#!/usr/bin/env python3
"""
Test script for exposure calculator with real eclipse data.
This demonstrates the complete workflow of calculating exposures for an eclipse.
"""

from astropy.time import Time
from solareclipseworkbench.exposure_calculator import (
    calculate_eclipse_exposures,
    calculate_exposure,
    format_shutter_speed
)

def test_exposure_calculator():
    """Test the exposure calculator with 2026 Spain eclipse."""
    print("=" * 70)
    print("Eclipse Exposure Calculator Test")
    print("=" * 70)
    
    # Eclipse: August 12, 2026 - Total Solar Eclipse
    # Location: Spain (near Zaragoza)
    eclipse_date = Time('2026-08-12')
    longitude = -3.9852
    latitude = 41.6669
    altitude = 828.0  # meters above sea level
    
    # Camera settings
    iso = 400
    aperture = 5.6
    nd_filter = 5.0  # ND5.0 solar filter for partial phases
    
    print(f"\nTest Configuration:")
    print(f"  Eclipse Date: August 12, 2026 (Total Solar Eclipse)")
    print(f"  Location: {latitude:.4f}°N, {longitude:.4f}°E")
    print(f"  Altitude: {altitude:.1f} m above sea level")
    print(f"  Camera: ISO {iso}, f/{aperture}")
    print(f"  Solar Filter: ND{nd_filter}")
    print()
    
    # Calculate all exposures for this eclipse
    try:
        exposures = calculate_eclipse_exposures(
            eclipse_date, longitude, latitude, altitude,
            iso, aperture, nd_filter
        )
        
        print("Calculated Exposures:")
        print("-" * 70)
        print(f"{'Phenomenon':<30} {'Shutter':<12} {'Sun Alt':<10} {'Local Time'}")
        print("-" * 70)
        
        for name, data in exposures.items():
            sun_alt = data['sun_altitude']
            shutter = data['shutter']
            time_local = data['time_local'].strftime('%H:%M:%S')
            print(f"{name:<30} {shutter:<12} {sun_alt:>5.1f}°     {time_local}")
        
        print("-" * 70)
        print(f"\nTotal phenomena calculated: {len(exposures)}")
        
        # Test individual calculations for different sun angles
        print("\n" + "=" * 70)
        print("Sun Altitude Effect Demonstration")
        print("=" * 70)
        print("\nBaily's Beads exposure at different sun altitudes:")
        print("(ISO 400, f/5.6, 1000m observer altitude)")
        print()
        
        test_sun_angles = [0, 15, 30, 45, 60]
        for sun_angle in test_sun_angles:
            exp = calculate_exposure("bailys_beads", sun_angle, 1000, iso, aperture)
            shutter = format_shutter_speed(exp)
            print(f"  Sun at {sun_angle:2d}°: {shutter:>10s}")
        
        print("\n" + "=" * 70)
        print("Observer Altitude Effect Demonstration")  
        print("=" * 70)
        print("\nBaily's Beads exposure at different observer altitudes:")
        print("(ISO 400, f/5.6, sun at 10° altitude)")
        print()
        
        test_altitudes = [0, 1000, 2000, 3000]
        for obs_alt in test_altitudes:
            exp = calculate_exposure("bailys_beads", 10, obs_alt, iso, aperture)
            shutter = format_shutter_speed(exp)
            print(f"  Observer at {obs_alt:4d}m: {shutter:>10s}")
        
        print("\n" + "=" * 70)
        print("ISO Sensitivity Effect Demonstration")
        print("=" * 70)
        print("\nCorona middle exposure at different ISO values:")
        print("(f/5.6, sun at 45°, 1000m observer altitude)")
        print()
        
        test_isos = [100, 200, 400, 800, 1600, 3200]
        for test_iso in test_isos:
            exp = calculate_exposure("corona_middle", 45, 1000, test_iso, aperture)
            shutter = format_shutter_speed(exp)
            print(f"  ISO {test_iso:4d}: {shutter:>10s}")
        
        print("\n" + "=" * 70)
        print("✓ All tests passed successfully!")
        print("=" * 70)
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error during calculation: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    import sys
    success = test_exposure_calculator()
    sys.exit(0 if success else 1)
