/* Compatibility shim for building PHP 7.0 with Visual C++ 14.0 (VS2015).

 * The UCRT that ships with current Windows SDKs uses _mm_loadu_si64 in
 * <wchar.h>. That intrinsic appeared in VS2017; VC14 treats it as an unknown
 * function and the header fails to compile:
 *
 *     error C2440: 'initializing': cannot convert from 'int' to '__m128i'
 *
 * The SSE2 load below has existed since VS2012 and produces the same value
 * for the 8-byte load the UCRT performs.
 *
 * Forced into every translation unit with /FI from the Makefile, because the
 * offending header is included by the C runtime itself.
 */
#ifndef SERVBAY_VC14_INTRINSICS_H
#define SERVBAY_VC14_INTRINSICS_H

#if defined(_MSC_VER) && _MSC_VER < 1910
#include <emmintrin.h>
#ifndef _mm_loadu_si64
#define _mm_loadu_si64(p) _mm_loadl_epi64((const __m128i *)(p))
#endif
#endif

#endif /* SERVBAY_VC14_INTRINSICS_H */
