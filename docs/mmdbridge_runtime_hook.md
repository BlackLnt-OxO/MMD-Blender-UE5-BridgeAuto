# MMDBridge Runtime CBT startup fix

The verified upstream `d3d9.dll` calls
`SetWindowsHookExW(WH_CBT, CbtHookProc, hInstance, GetCurrentThreadId())`.
This hook only observes the current MMD thread. The
[Windows API contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw)
requires a null module handle for a hook in the current process's thread.
The [upstream implementation](https://github.com/rintrint/mmdbridge/blob/master/src/d3d9/d3d9.cpp)
uses it to observe creation and destruction of the AVI recording window.

In the failing copied Runtime, the original call returned NULL with Windows
error 126. Changing only the module argument to NULL in the debugger made
the same copy return a valid hook handle with error 0. This confirms the
parameter correction; it does not establish why Windows accepted some
previously used paths while rejecting new paths in that session.

`mmd_bridge.mmd_avi.runtime_hook_dll` applies this correction only to the
Runtime copy of the exact verified upstream build:

| Property | Value |
| --- | --- |
| Original DLL SHA-256 | `74f27cee28b0060d5b79b73b66fa665c3500eb19aa65e1baacc9f97c54e637a3` |
| File offset | `0x59672` (RVA `0x5a272`) |
| Original instruction | `4c 8b 05 f7 95 06 00` (`mov r8,[hInstance]`) |
| Replacement | `45 33 c0 90 90 90 90` (`xor r8d,r8d`, four NOPs) |

The callback, hook type, thread ID, error handling and recording logic are
unchanged. The original installation is never modified. Unknown DLL builds
are left untouched; their compatibility must be verified separately.
Runtime cache integrity checks compare against the corrected DLL hash,
while `source.json` retains both original and Runtime hashes. A corrected
cache is retained across tasks rather than copied again for each bake.

The equivalent upstream source change is:

```diff
- g_hCbtHook = SetWindowsHookEx(WH_CBT, CbtHookProc, hInstance, GetCurrentThreadId());
+ g_hCbtHook = SetWindowsHookEx(WH_CBT, CbtHookProc, NULL, GetCurrentThreadId());
```

Local diagnostic and native bake receipts are under
`work/multi_character_20261016/`. They are validation artifacts, not bundled
third-party DLLs or redistributable model assets.
