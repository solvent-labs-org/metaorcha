"""Pure-Python Ed25519 after RFC 8032 section 6 (the reference code), stdlib only.
E1 v0, on-device APK payload only: slow and not constant-time (see the package
docstring and android/payload/NOTICE-crypto-pure-ed25519.txt)."""
import hashlib

p = 2**255 - 19
q = 2**252 + 27742317777372353535851937790883648493

def _inv(x):
    return pow(x, p - 2, p)

d = (-121665 * _inv(121666)) % p
modp_sqrt_m1 = pow(2, (p - 1) // 4, p)

def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * _inv(d * y * y + 1)
    if x2 == 0:
        if sign:
            return None
        return 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * modp_sqrt_m1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x

g_y = 4 * _inv(5) % p
g_x = _recover_x(g_y, 0)
G = (g_x, g_y, 1, g_x * g_y % p)

def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, Gg, H = B - A, D - C, D + C, B + A
    return (E * F, Gg * H, F * Gg, E * H)

def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q

def _equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % p != 0:
        return False
    return True

def _compress(P):
    zinv = _inv(P[2])
    x = P[0] * zinv % p
    y = P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")

def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)

def _sha512_int(m):
    return int.from_bytes(hashlib.sha512(m).digest(), "little")

def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]

def public_key(secret):
    a, _ = _expand(secret)
    return _compress(_mul(a, G))

def sign(secret, msg):
    a, prefix = _expand(secret)
    A = _compress(_mul(a, G))
    r = _sha512_int(prefix + msg) % q
    Rs = _compress(_mul(r, G))
    h = _sha512_int(Rs + A + msg) % q
    s = (r + h * a) % q
    return Rs + int.to_bytes(s, 32, "little")

def verify(public, msg, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    if not A:
        return False
    Rs = signature[:32]
    R = _decompress(Rs)
    if not R:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= q:
        return False
    h = _sha512_int(Rs + public + msg) % q
    sB = _mul(s, G)
    hA = _mul(h, A)
    return _equal(sB, _add(R, hA))
