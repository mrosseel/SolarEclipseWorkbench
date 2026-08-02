#!/usr/bin/env python3
"""
Test script to verify partial phase generation with all intermediate shots.
"""

from datetime import timedelta
from astropy.time import Time
from solareclipseworkbench.reference_moments import calculate_reference_moments
from solareclipseworkbench.exposure_calculator import (
    calculate_sun_altitude_at_time,
    calculate_exposure,
    format_shutter_speed
)

def test_partial_phase_generation():
    """Test generation of all partial phase shots."""
    print("=" * 80)
    print("Partial Phase Shot Generation Test")
    print("=" * 80)
    
    # Eclipse: August 12, 2026 - Total Solar Eclipse in Spain
    eclipse_date = Time('2026-08-12')
    longitude = -3.9852
    latitude = 41.6669
    altitude = 828.0
    
    # Camera settings
    iso = 400
    aperture = 5.6
    nd_filter = 5.0
    max_iso = 1600
    
    print(f"\nTest Configuration:")
    print(f"  Eclipse: August 12, 2026 (Total Solar Eclipse)")
    print(f"  Location: {latitude:.4f}°N, {longitude:.4f}°E, {altitude:.1f}m")
    print(f"  Camera: ISO {iso}, f/{aperture}, ND{nd_filter}")
    print(f"  Max ISO: {max_iso}")
    print()
    
    # Get reference moments
    timings, magnitude, eclipse_type = calculate_reference_moments(
        longitude, latitude, altitude, eclipse_date
    )
    
    print(f"Eclipse Type: {eclipse_type}")
    print(f"Magnitude: {magnitude:.4f}")
    print()
    
    if 'C1' not in timings or 'C2' not in timings:
        print("ERROR: C1 or C2 not found in timings!")
        return False
    
    c1_time = timings['C1'].time_utc
    c2_time = timings['C2'].time_utc
    
    print("Reference Moments:")
    print(f"  C1: {c1_time.strftime('%H:%M:%S UTC')} (sun altitude: {timings['C1'].altitude:.1f}°)")
    print(f"  C2: {c2_time.strftime('%H:%M:%S UTC')} (sun altitude: {timings['C2'].altitude:.1f}°)")
    
    if 'C3' in timings:
        c3_time = timings['C3'].time_utc
        print(f"  C3: {c3_time.strftime('%H:%M:%S UTC')} (sun altitude: {timings['C3'].altitude:.1f}°)")
    if 'C4' in timings:
        c4_time = timings['C4'].time_utc
        print(f"  C4: {c4_time.strftime('%H:%M:%S UTC')} (sun altitude: {timings['C4'].altitude:.1f}°)")
    
    # Calculate C1-C2 duration
    c1_c2_duration = (c2_time - c1_time).total_seconds()
    print(f"\nC1 to C2 Duration: {c1_c2_duration:.0f} seconds ({c1_c2_duration/60:.1f} minutes)")
    
    # Test with 60 second intervals
    time_interval = 60  # seconds
    num_shots = int(c1_c2_duration / time_interval)
    
    print(f"\nGenerating shots every {time_interval} seconds")
    print(f"Expected number of shots: {num_shots}")
    print()
    print("-" * 80)
    print(f"{'Shot':<6} {'Time (UTC)':<12} {'Offset':<10} {'Sun Alt':<10} {'Shutter':<12} {'ISO':<6} {'Notes'}")
    print("-" * 80)
    
    shots_generated = 0
    for i in range(num_shots):
        shot_time = c1_time + timedelta(seconds=i * time_interval)
        if shot_time >= c2_time:
            break
        
        # Calculate sun altitude at this time
        sun_alt = calculate_sun_altitude_at_time(
            shot_time, eclipse_date, longitude, latitude, altitude
        )
        
        # Calculate exposure
        exposure = calculate_exposure(
            "partial", sun_alt, altitude, iso, aperture, nd_filter
        )
        
        # Auto-adjust ISO if needed
        adjusted_iso = iso
        iso_adjusted = False
        
        # If exposure > 1/30s, increase ISO
        while exposure > 1/30 and adjusted_iso < max_iso:
            adjusted_iso *= 2
            if adjusted_iso > max_iso:
                adjusted_iso = max_iso
                break
            exposure = calculate_exposure(
                "partial", sun_alt, altitude, adjusted_iso, aperture, nd_filter
            )
            iso_adjusted = True
        
        shutter = format_shutter_speed(exposure)
        
        # Calculate offset from C1
        offset_seconds = (shot_time - c1_time).total_seconds()
        offset_str = f"{int(offset_seconds // 60)}:{int(offset_seconds % 60):02d}"
        
        time_str = shot_time.strftime('%H:%M:%S')
        notes = "ISO adjusted" if iso_adjusted else ""
        
        print(f"#{i+1:<5} {time_str:<12} {offset_str:<10} {sun_alt:>6.1f}°   {shutter:<12} {adjusted_iso:<6} {notes}")
        shots_generated += 1
    
    print("-" * 80)
    print(f"\nTotal shots generated: {shots_generated}")
    
    # Show the sun altitude progression
    print("\n" + "=" * 80)
    print("Sun Altitude Progression Analysis")
    print("=" * 80)
    
    sample_times = [
        ("C1", c1_time, timings['C1'].altitude),
        ("C1+15min", c1_time + timedelta(minutes=15), None),
        ("C1+30min", c1_time + timedelta(minutes=30), None),
        ("C2-15min", c2_time - timedelta(minutes=15), None),
        ("C2-5min", c2_time - timedelta(minutes=5), None),
        ("C2", c2_time, timings['C2'].altitude)
    ]
    
    print(f"\n{'Moment':<12} {'Time (UTC)':<12} {'Sun Alt':<10} {'Exposure (ISO {iso}, f/{aperture}, ND{nd_filter})'}")
    print("-" * 80)
    
    for label, time, known_alt in sample_times:
        if time < c2_time:
            if known_alt is not None:
                sun_alt = known_alt
            else:
                sun_alt = calculate_sun_altitude_at_time(
                    time, eclipse_date, longitude, latitude, altitude
                )
            
            exposure = calculate_exposure(
                "partial", sun_alt, altitude, iso, aperture, nd_filter
            )
            shutter = format_shutter_speed(exposure)
            time_str = time.strftime('%H:%M:%S')
            
            print(f"{label:<12} {time_str:<12} {sun_alt:>6.1f}°   {shutter}")
    
    print("\n" + "=" * 80)
    print("✓ Test completed successfully!")
    print("=" * 80)
    
    return True


if __name__ == "__main__":
    import sys
    success = test_partial_phase_generation()
    sys.exit(0 if success else 1)
