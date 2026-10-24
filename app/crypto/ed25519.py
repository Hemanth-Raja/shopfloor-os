"""Ed25519 signature verification (RFC 8032), standard library only. Verification only: we never sign.

A direct transcription of the RFC's reference arithmetic (extended twisted Edwards coordinates). Used to check
Meterless Nightshift's signed compute receipts without adding a crypto dependency.
"""
import hashlib
p = 2**255 - 19
q = 2**252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
I = pow(2, (p - 1) // 4, p)
def inv(x): return pow(x, p - 2, p)
def xrecover(y):
    xx = (y * y - 1) * inv(d * y * y + 1)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p != 0: x = (x * I) % p
    if x % 2 != 0: x = p - x
    return x
By = 4 * inv(5) % p
B = (xrecover(By), By, 1, xrecover(By) * By % p)
def add(P, Q):
    x1, y1, z1, t1 = P; x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % p; b = (y1 + x1) * (y2 + x2) % p
    c = 2 * t1 * t2 * d % p; dd = 2 * z1 * z2 % p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % p, g * h % p, f * g % p, e * h % p)
def mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1: Q = add(Q, P)
        P = add(P, P); s >>= 1
    return Q
def eq(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0
def decode(s):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = xrecover(y)
    if (x & 1) != (s[31] >> 7): x = p - x
    P = (x, y, 1, x * y % p)
    if (-x * x + y * y - 1 - d * x * x * y * y) % p != 0: raise ValueError("not on curve")
    return P
def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(sig) != 64 or len(pub) != 32: return False
    try:
        A, R = decode(pub), decode(sig[:32])
    except ValueError:
        return False
    S = int.from_bytes(sig[32:], "little")
    if S >= q: return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % q
    return eq(mul(S, B), add(R, mul(h, A)))
