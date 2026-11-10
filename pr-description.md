## Problem

- Nothing pins the upstream Omi source, and its emulator harness mapping to our Docker setup is undocumented
- Re-deriving env, ports, and routing by hand per submodule bump is slow and error-prone

## Solution

- Add BasedHardware/omi as a pinned git submodule
- Document upstream env stages, port remaps, LLM alias plan, and open questions in AGENTS.md
