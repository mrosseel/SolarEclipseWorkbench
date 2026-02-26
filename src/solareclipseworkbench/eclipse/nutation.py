# nutation.py - Python translation of nutation.js
# Adapted from Essential Routines for Fundamental Astronomy (ERFA)
# https://github.com/liberfa/erfa
import math

# Constants for astronomical calculations
DJC = 36525.0  # Days per Julian century
DJM = 365250.0 # Days per Julian millennium
DJ00 = 2451545.0 # Reference epoch (J2000.0), Julian date
D2PI = 6.283185307179586476925287 # 2 * pi
DAS2R = 4.848136811095359935899141e-6 # Arcseconds to radians
TURNAS = 1296000.0 # Arcseconds in a full circle

# Helper for floating point modulus
fmod = math.fmod

def _anp(a):
    """
    Normalize angle into the range 0 to 2*pi radians.
    Args:
        a (float): Angle in radians.
    Returns:
        float: Angle normalized to [0, 2*pi).
    """
    w = a % D2PI
    if w < 0:
        w += D2PI
    return w

class Nutation:
    """
    Class containing static methods for nutation and related astronomical calculations.
    Implements routines adapted from ERFA/IAU SOFA standards.
    """
    @staticmethod
    def era_gst00b(uta, utb):
        """
        Compute Greenwich apparent sidereal time (IAU 2000B).
        Args:
            uta, utb (float): UT1 as a two-part Julian date.
        Returns:
            float: Greenwich apparent sidereal time in radians.
        """
        gmst00 = Nutation.era_gmst00(uta, utb, uta, utb)
        ee00b = Nutation.era_ee00b(uta, utb)
        gst = _anp(gmst00 + ee00b)
        return gst

    @staticmethod
    def era_gmst00(uta, utb, tta, ttb):
        """
        Compute Greenwich mean sidereal time (IAU 2000).
        Args:
            uta, utb (float): UT1 as a two-part Julian date.
            tta, ttb (float): TT as a two-part Julian date.
        Returns:
            float: Greenwich mean sidereal time in radians.
        """
        t = ((tta - DJ00) + ttb) / DJC
        gmst = _anp(Nutation.era_era00(uta, utb) +
                    (0.014506 +
             (4612.15739966 +
              (1.39667721 +
               (-0.00009344 +
                0.00001882 * t) * t) * t) * t) * DAS2R)
        return gmst

    @staticmethod
    def era_era00(dj1, dj2):
        """
        Compute Earth Rotation Angle (IAU 2000).
        Args:
            dj1, dj2 (float): UT1 as a two-part Julian date.
        Returns:
            float: Earth rotation angle in radians.
        """
        d1, d2 = (dj1, dj2) if dj1 < dj2 else (dj2, dj1)
        t = d1 + (d2 - DJ00)
        f = fmod(d1, 1.0) + fmod(d2, 1.0)
        theta = _anp(D2PI * (f + 0.7790572732640 + 0.00273781191135448 * t))
        return theta

    @staticmethod
    def era_ee00b(date1, date2):
        """
        Compute equation of the equinoxes, IAU 2000B model.
        Args:
            date1, date2 (float): TT as a two-part Julian date.
        Returns:
            float: Equation of the equinoxes in radians.
        """
        pr = Nutation.era_pr00(date1, date2)
        dps_ipr, dep_spr = pr[0], pr[1]
        epsa = Nutation.era_obl80(date1, date2) + dep_spr
        t = ((date1 - DJ00) + date2) / DJC
        nut = Nutation.nutation(t)
        dpsi, deps = nut[0], nut[1]
        ee = Nutation.era_ee00(date1, date2, epsa, dpsi)
        return ee

    @staticmethod
    def era_obl80(date1, date2):
        """
        Mean obliquity of the ecliptic, IAU 1980 model.
        Args:
            date1, date2 (float): TT as a two-part Julian date.
        Returns:
            float: Mean obliquity in radians.
        """
        t = ((date1 - DJ00) + date2) / DJC
        eps0 = DAS2R * (84381.448 +
                        (-46.8150 +
                         (-0.00059 +
                          0.001813 * t) * t) * t)
        return eps0

    @staticmethod
    def era_pr00(date1, date2):
        """
        Precession-rate adjustments (IAU 2000).
        Args:
            date1, date2 (float): TT as a two-part Julian date.
        Returns:
            list: [dpsipr, depspr] corrections in radians.
        """
        pre_cor = -0.29965 * DAS2R
        obj_cor = -0.02524 * DAS2R
        t = ((date1 - DJ00) + date2) / DJC
        return [pre_cor * t, obj_cor * t]

    @staticmethod
    def era_ee00(date1, date2, epsa, dpsi):
        """
        Equation of the equinoxes, IAU 2000 model.
        Args:
            date1, date2 (float): TT as a two-part Julian date.
            epsa (float): True obliquity in radians.
            dpsi (float): Nutation in longitude in radians.
        Returns:
            float: Equation of the equinoxes in radians.
        """
        ee = dpsi * math.cos(epsa) + Nutation.era_eect00(date1, date2)
        return ee

    @staticmethod
    def era_eect00(date1, date2):
        """
        Equation of the equinoxes complementary terms (IAU 2000).
        Args:
            date1, date2 (float): TT as a two-part Julian date.
        Returns:
            float: Complementary terms in radians.
        """
        # Only the t^0 and t^1 terms are included, as in the JS
        t = ((date1 - DJ00) + date2) / DJC
        # Fundamental arguments
        fa = [Nutation.era_fal03(t), Nutation.era_falp03(t), Nutation.era_faf03(t),
              Nutation.era_fad03(t), Nutation.era_faom03(t), Nutation.era_fave03(t),
              Nutation.era_fae03(t), Nutation.era_fapa03(t)]
        # e0 and e1 terms (see JS for details)
        e0 = [
            ([0,0,0,0,1,0,0,0], 2640.96e-6, -0.39e-6),
            ([0,0,0,0,2,0,0,0], 63.52e-6, -0.02e-6),
            ([0,0,2,-2,3,0,0,0], 11.75e-6, 0.01e-6),
            ([0,0,2,-2,1,0,0,0], 11.21e-6, 0.01e-6),
            ([0,0,2,-2,2,0,0,0], -4.55e-6, 0.00e-6),
            ([0,0,2,0,3,0,0,0], 2.02e-6, 0.00e-6),
            ([0,0,2,0,1,0,0,0], 1.98e-6, 0.00e-6),
            ([0,0,0,0,3,0,0,0], -1.72e-6, 0.00e-6),
            ([0,1,0,0,1,0,0,0], -1.41e-6, -0.01e-6),
            ([0,1,0,0,-1,0,0,0], -1.26e-6, -0.01e-6),
            ([1,0,0,0,-1,0,0,0], -0.63e-6, 0.00e-6),
            ([1,0,0,0,1,0,0,0], -0.63e-6, 0.00e-6),
            ([0,1,2,-2,3,0,0,0], 0.46e-6, 0.00e-6),
            ([0,1,2,-2,1,0,0,0], 0.45e-6, 0.00e-6),
            ([0,0,4,-4,4,0,0,0], 0.36e-6, 0.00e-6),
            ([0,0,1,-1,1,-8,12,0], -0.24e-6, -0.12e-6),
            ([0,0,2,0,0,0,0,0], 0.32e-6, 0.00e-6),
            ([0,0,2,0,2,0,0,0], 0.28e-6, 0.00e-6),
            ([1,0,2,0,3,0,0,0], 0.27e-6, 0.00e-6),
            ([1,0,2,0,1,0,0,0], 0.26e-6, 0.00e-6),
            ([0,0,2,-2,0,0,0,0], -0.21e-6, 0.00e-6),
            ([0,1,-2,2,-3,0,0,0], 0.19e-6, 0.00e-6),
            ([0,1,-2,2,-1,0,0,0], 0.18e-6, 0.00e-6),
            ([0,0,0,0,0,8,-13,-1], -0.10e-6, 0.05e-6),
            ([0,0,0,2,0,0,0,0], 0.15e-6, 0.00e-6),
            ([2,0,-2,0,-1,0,0,0], -0.14e-6, 0.00e-6),
            ([1,0,0,-2,1,0,0,0], 0.14e-6, 0.00e-6),
            ([0,1,2,-2,2,0,0,0], -0.14e-6, 0.00e-6),
            ([1,0,0,-2,-1,0,0,0], 0.14e-6, 0.00e-6),
            ([0,0,4,-2,4,0,0,0], 0.13e-6, 0.00e-6),
            ([0,0,2,-2,4,0,0,0], -0.11e-6, 0.00e-6),
            ([1,0,-2,0,-3,0,0,0], 0.11e-6, 0.00e-6),
            ([1,0,-2,0,-1,0,0,0], 0.11e-6, 0.00e-6)
        ]
        e1 = [([0,0,0,0,1,0,0,0], -0.87e-6, 0.00e-6)]
        s0 = 0.0
        s1 = 0.0
        for row in e0:
            a = sum(row[0][j] * fa[j] for j in range(8))
            s0 += row[1] * math.sin(a) + row[2] * math.cos(a)
        for row in e1:
            a = sum(row[0][j] * fa[j] for j in range(8))
            s1 += row[1] * math.sin(a) + row[2] * math.cos(a)
        eect = (s0 + s1 * t) * DAS2R
        return eect

    @staticmethod
    def era_fapa03(t):
        """
        General accumulated precession in longitude (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Precession in radians.
        """
        return (0.024381750 + 0.00000538691 * t) * t

    @staticmethod
    def era_fae03(t):
        """
        Mean anomaly of the Earth (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Mean anomaly in radians.
        """
        return fmod(1.753470314 + 628.3075849991 * t, D2PI)

    @staticmethod
    def era_fave03(t):
        """
        Mean longitude of Venus (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Mean longitude in radians.
        """
        return fmod(3.176146697 + 1021.3285546211 * t, D2PI)

    @staticmethod
    def era_faom03(t):
        """
        Mean longitude of the Moon's ascending node (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Longitude in radians.
        """
        return fmod(450160.398036 + t * (-6962890.5431 + t * (7.4722 + t * (0.007702 + t * (-0.00005939)))), TURNAS) * DAS2R

    @staticmethod
    def era_fad03(t):
        """
        Mean elongation of the Moon from the Sun (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Elongation in radians.
        """
        return fmod(1072260.703692 + t * (1602961601.2090 + t * (-6.3706 + t * (0.006593 + t * (-0.00003169)))), TURNAS) * DAS2R

    @staticmethod
    def era_faf03(t):
        """
        Mean argument of latitude of the Moon (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Argument of latitude in radians.
        """
        return fmod(335779.526232 + t * (1739527262.8478 + t * (-12.7512 + t * (-0.001037 + t * 0.00000417))), TURNAS) * DAS2R

    @staticmethod
    def era_falp03(t):
        """
        Mean anomaly of the Moon (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Mean anomaly in radians.
        """
        return fmod(1287104.793048 + t * (129596581.0481 + t * (-0.5532 + t * (0.000136 + t * (-0.00001149)))), TURNAS) * DAS2R

    @staticmethod
    def era_fal03(t):
        """
        Mean anomaly of the Sun (IAU 2000).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            float: Mean anomaly in radians.
        """
        return fmod(485868.249036 + t * (1717915923.2178 + t * (31.8792 + t * (0.051635 + t * (-0.00024470)))), TURNAS) * DAS2R

    @staticmethod
    def nutation(t):
        """
        Compute nutation in longitude and obliquity (IAU 2000B model).
        Args:
            t (float): Julian centuries since J2000.0.
        Returns:
            list: [dpsi, deps] nutation in longitude and obliquity (radians).
        """
        # IAU 2000B Nutation
        nals = [
            [0,0,0,0,1], [0,0,2,-2,2], [0,0,2,0,2], [0,0,0,0,2], [0,1,0,0,0], [0,1,2,-2,2], [1,0,0,0,0], [0,0,2,0,1], [1,0,2,0,2], [0,-1,2,-2,2],
            [0,0,2,-2,1], [-1,0,2,0,2], [-1,0,0,2,0], [1,0,0,0,1], [-1,0,0,0,1], [-1,0,2,2,2], [1,0,2,0,1], [-2,0,2,0,1], [0,0,0,2,0], [0,0,2,2,2],
            [0,-2,2,-2,2], [-2,0,0,2,0], [2,0,2,0,2], [1,0,2,-2,2], [-1,0,2,0,1], [2,0,0,0,0], [0,0,2,0,0], [0,1,0,0,1], [-1,0,0,2,1], [0,2,2,-2,2],
            [0,0,-2,2,0], [1,0,0,-2,1], [0,-1,0,0,1], [-1,0,2,2,1], [0,2,0,0,0], [1,0,2,2,2], [-2,0,2,0,0], [0,1,2,0,2], [0,0,2,2,1], [0,-1,2,0,2],
            [0,0,0,2,1], [1,0,2,-2,1], [2,0,2,-2,2], [-2,0,0,2,1], [2,0,2,0,1], [0,-1,2,-2,1], [0,0,0,-2,1], [-1,-1,0,2,0], [2,0,0,-2,1], [1,0,0,2,0],
            [0,1,2,-2,1], [1,-1,0,0,0], [-2,0,2,0,2], [3,0,2,0,2], [0,-1,0,2,0], [1,-1,2,0,2], [0,0,0,1,0], [-1,-1,2,2,2], [-1,0,2,0,0], [0,-1,2,2,2],
            [-2,0,0,0,1], [1,1,2,0,2], [2,0,0,0,1], [-1,1,0,1,0], [1,1,0,0,0], [1,0,2,0,0], [-1,0,2,-2,1], [1,0,0,0,2], [-1,0,0,1,0], [0,0,2,1,2],
            [-1,0,2,4,2], [-1,1,0,1,1], [0,-2,2,-2,1], [1,0,2,2,1], [-2,0,2,2,2], [-1,0,0,0,2], [1,1,2,-2,2]
        ]
        cls = [
            [-172064161,-174666,33386,92052331,9086,15377],
            [-13170906,-1675,-13696,5730336,-3015,-4587],
            [-2276413,-234,2796,978459,-485,1374],
            [2074554,207,-698,-897492,470,-291],
            [1475877,-3633,11817,73871,-184,-1924],
            [-516821,1226,-524,224386,-677,-174],
            [711159,73,-872,-6750,0,358],
            [-387298,-367,380,200728,18,318],
            [-301461,-36,816,129025,-63,367],
            [215829,-494,111,-95929,299,132],
            [128227,137,181,-68982,-9,39],
            [123457,11,19,-53311,32,-4],
            [156994,10,-168,-1235,0,82],
            [63110,63,27,-33228,0,-9],
            [-57976,-63,-189,31429,0,-75],
            [-59641,-11,149,25543,-11,66],
            [-51613,-42,129,26366,0,78],
            [45893,50,31,-24236,-10,20],
            [63384,11,-150,-1220,0,29],
            [-38571,-1,158,16452,-11,68],
            [32481,0,0,-13870,0,0],
            [-47722,0,-18,477,0,-25],
            [-31046,-1,131,13238,-11,59],
            [28593,0,-1,-12338,10,-3],
            [20441,21,10,-10758,0,-3],
            [29243,0,-74,-609,0,13],
            [25887,0,-66,-550,0,11],
            [-14053,-25,79,8551,-2,-45],
            [15164,10,11,-8001,0,-1],
            [-15794,72,-16,6850,-42,-5],
            [21783,0,13,-167,0,13],
            [-12873,-10,-37,6953,0,-14],
            [-12654,11,63,6415,0,26],
            [-10204,0,25,5222,0,15],
            [16707,-85,-10,168,-1,10],
            [-7691,0,44,3268,0,19],
            [-11024,0,-14,104,0,2],
            [7566,-21,-11,-3250,0,-5],
            [-6637,-11,25,3353,0,14],
            [-7141,21,8,3070,0,4],
            [-6302,-11,2,3272,0,4],
            [5800,10,2,-3045,0,-1],
            [6443,0,-7,-2768,0,-4],
            [-5774,-11,-15,3041,0,-5],
            [-5350,0,21,2695,0,12],
            [-4752,-11,-3,2719,0,-3],
            [-4940,-11,-21,2720,0,-9],
            [7350,0,-8,-51,0,4],
            [4065,0,6,-2206,0,1],
            [6579,0,-24,-199,0,2],
            [3579,0,5,-1900,0,1],
            [4725,0,-6,-41,0,3],
            [-3075,0,-2,1313,0,-1],
            [-2904,0,15,1233,0,7],
            [4348,0,-10,-81,0,2],
            [-2878,0,8,1232,0,4],
            [-4230,0,5,-20,0,-2],
            [-2819,0,7,1207,0,3],
            [-4056,0,5,40,0,-2],
            [-2647,0,11,1129,0,5],
            [-2294,0,-10,1266,0,-4],
            [2481,0,-7,-1062,0,-3],
            [2179,0,-2,-1129,0,-2],
            [3276,0,1,-9,0,0],
            [-3389,0,5,35,0,-2],
            [3339,0,-13,-107,0,1],
            [-1987,0,-6,1073,0,-2],
            [-1981,0,0,854,0,0],
            [4026,0,-353,-553,0,-139],
            [1660,0,-5,-710,0,-2],
            [-1521,0,9,647,0,4],
            [1314,0,0,-700,0,0],
            [-1283,0,0,672,0,0],
            [-1331,0,8,663,0,4],
            [1383,0,-2,-594,0,-2],
            [1405,0,4,-610,0,2],
            [1290,0,0,-556,0,0]
        ]
        u2_r = DAS2R/1E7
        dmas2_r = DAS2R / 1E3
        dp_plan = -0.135 * dmas2_r
        de_plan = 0.388 * dmas2_r
        el = ((485868.249036 + t * 1717915923.2178) % TURNAS) * DAS2R
        elp = ((1287104.79305 + t * 129596581.0481) % TURNAS) * DAS2R
        f = ((335779.526232 + t * 1739527262.8478) % TURNAS) * DAS2R
        d = ((1072260.70369 + t * 1602961601.2090) % TURNAS) * DAS2R
        om = ((450160.398036 - t * 6962890.5431) % TURNAS) * DAS2R
        dp = 0
        de = 0
        for I in range(len(nals)-1, -1, -1):
            arg = (nals[I][0] * el + nals[I][1] * elp + nals[I][2] * f + nals[I][3] * d + nals[I][4] * om) % D2PI
            s_arg = math.sin(arg)
            c_arg = math.cos(arg)
            dp += (cls[I][0] + cls[I][1] * t) * s_arg + cls[I][2] * c_arg
            de += (cls[I][3] + cls[I][4] * t) * c_arg + cls[I][5] * s_arg
        dpsils = dp * u2_r
        depsls = de * u2_r
        dpsipl = dp_plan
        depspl = de_plan
        return [dpsipl + dpsils, depspl + depsls]
