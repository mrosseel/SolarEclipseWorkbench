# vec.py - Python translation of Vec.js
# Simplified Vector/Matrix library for 3D vectors and 3x3 matrices
import math

class Vec:
    """
    A static class providing basic vector and matrix operations for 3D vectors and 3x3 matrices.
    """
    @staticmethod
    def transpose(m):
        """
        Transpose a matrix (swap rows and columns).
        Args:
            m (list of list of float): Matrix to transpose.
        Returns:
            list of list of float: Transposed matrix.
        """
        return [list(row) for row in zip(*m)]

    @staticmethod
    def vec_matrix_mul(v, m):
        """
        Multiply a vector by a 3x3 matrix. Supports 3- or 6-element vectors.
        Args:
            v (list of float): Vector (length 3 or 6).
            m (list of list of float): 3x3 matrix.
        Returns:
            list of float: Resulting vector.
        """
        t = [
            v[0] * m[0][0] + v[1] * m[0][1] + v[2] * m[0][2],
            v[0] * m[1][0] + v[1] * m[1][1] + v[2] * m[1][2],
            v[0] * m[2][0] + v[1] * m[2][1] + v[2] * m[2][2]
        ]
        if len(v) > 3:
            t += [
                v[3] * m[0][0] + v[4] * m[0][1] + v[5] * m[0][2],
                v[3] * m[1][0] + v[4] * m[1][1] + v[5] * m[1][2],
                v[3] * m[2][0] + v[4] * m[2][1] + v[5] * m[2][2]
            ]
        return t

    @staticmethod
    def vec_dot(a, b):
        """
        Compute the dot product of two 3-element vectors.
        Args:
            a (list of float): First vector.
            b (list of float): Second vector.
        Returns:
            float: Dot product.
        """
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    @staticmethod
    def sub(a, b):
        """
        Subtract vector b from vector a (element-wise).
        Args:
            a (list of float): First vector.
            b (list of float): Second vector.
        Returns:
            list of float: Resulting vector.
        """
        return [a[i] - b[i] for i in range(min(len(a), len(b)))]

    @staticmethod
    def add(a, b):
        """
        Add two vectors (element-wise).
        Args:
            a (list of float): First vector.
            b (list of float): Second vector.
        Returns:
            list of float: Resulting vector.
        """
        return [a[i] + b[i] for i in range(min(len(a), len(b)))]

    @staticmethod
    def scalar_mul(v, a):
        """
        Multiply a vector by a scalar.
        Args:
            v (list of float): Vector.
            a (float): Scalar value.
        Returns:
            list of float: Resulting vector.
        """
        return [x * a for x in v]

    @staticmethod
    def magnitude(a):
        """
        Compute the magnitude (Euclidean norm) of a 3-element vector.
        Args:
            a (list of float): Vector.
        Returns:
            float: Magnitude of the vector.
        """
        return math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)

    @staticmethod
    def get_x_rotation_matrix(r):
        """
        Get a 3x3 rotation matrix for rotation about the X-axis.
        Args:
            r (float): Angle in radians.
        Returns:
            list of list of float: 3x3 rotation matrix.
        """
        return [
            [1, 0, 0],
            [0, math.cos(r), math.sin(r)],
            [0, -math.sin(r), math.cos(r)]
        ]

    @staticmethod
    def get_y_rotation_matrix(r):
        """
        Get a 3x3 rotation matrix for rotation about the Y-axis.
        Args:
            r (float): Angle in radians.
        Returns:
            list of list of float: 3x3 rotation matrix.
        """
        return [
            [math.cos(r), 0, -math.sin(r)],
            [0, 1, 0],
            [math.sin(r), 0, math.cos(r)]
        ]

    @staticmethod
    def get_z_rotation_matrix(r):
        """
        Get a 3x3 rotation matrix for rotation about the Z-axis.
        Args:
            r (float): Angle in radians.
        Returns:
            list of list of float: 3x3 rotation matrix.
        """
        return [
            [math.cos(r), math.sin(r), 0],
            [-math.sin(r), math.cos(r), 0],
            [0, 0, 1]
        ]

    @staticmethod
    def dot(a, b):
        """
        Matrix dot product (matrix multiplication) for two matrices.
        Args:
            a (list of list of float): First matrix.
            b (list of list of float): Second matrix.
        Returns:
            list of list of float: Resulting matrix.
        """
        m = []
        for i in range(len(a)):
            row = []
            for j in range(len(b[0])):
                temp = 0
                for k in range(len(b)):
                    temp += a[i][k] * b[k][j]
                row.append(temp)
            m.append(row)
        return m

class PolynomialRegression:
    """
    Static class for solving polynomial regression using Gaussian elimination.
    """
    @staticmethod
    def solve(x, y, degree):
        """
        Solve for polynomial regression coefficients of a given degree.
        Args:
            x (list of float): Input x values.
            y (list of float): Input y values.
            degree (int): Degree of the polynomial.
        Returns:
            list of float: Polynomial coefficients (lowest to highest degree).
        """
        a = []
        for i in range(len(x)):
            row = [x[i] ** j for j in range(degree + 1)]
            row.append(float(y[i]))
            a.append(row)
        col = 0
        row = 0
        while row < len(a) and col < (len(a[0]) - 1):
            PolynomialRegression.find_non_zero_pivot(a, row, col)
            if a[row][col] != 0:
                PolynomialRegression.make_pivot_one(a, row, col)
                PolynomialRegression.make_cols_zero(a, row, col)
                row += 1
            col += 1
        ret = [a[i][len(a[i]) - 1] for i in range(degree + 1)]
        return ret

    @staticmethod
    def make_cols_zero(a, row, col):
        """
        Make all elements in a column zero except for the pivot row (used in Gaussian elimination).
        Args:
            a (list of list of float): Augmented matrix.
            row (int): Pivot row index.
            col (int): Pivot column index.
        """
        for i in range(len(a)):
            if i != row:
                t = a[i][col]
                for j in range(len(a[i])):
                    a[i][j] -= t * a[row][j]

    @staticmethod
    def make_pivot_one(a, row, col):
        """
        Normalize the pivot row so the pivot element becomes one.
        Args:
            a (list of list of float): Augmented matrix.
            row (int): Pivot row index.
            col (int): Pivot column index.
        """
        t = a[row][col]
        for i in range(len(a[row])):
            a[row][i] /= t

    @staticmethod
    def find_non_zero_pivot(a, row, col):
        """
        Find a non-zero pivot in a column and swap rows if necessary.
        Args:
            a (list of list of float): Augmented matrix.
            row (int): Current row index.
            col (int): Current column index.
        """
        i = row
        while i < len(a) and a[i][col] == 0:
            i += 1
        if i != row and i < len(a):
            a[row], a[i] = a[i], a[row]

# End of vec.py
