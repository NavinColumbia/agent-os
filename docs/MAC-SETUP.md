# Adding your Mac as an iOS/macOS runner

The control plane stays on the WSL box. The Mac becomes a remote executor over your Tailscale tailnet,
reached by `scripts/mac_runner.py` over SSH. ~10 minutes of one-time setup on the Mac.

## On the Mac (you do this)
1. **Install Tailscale** (Mac App Store or tailscale.com/download), sign in to the **same account**
   (artmusicasia@gmail.com) so the Mac joins your tailnet. Note its name (e.g. `navins-macbook`).
2. **Enable SSH:** System Settings → General → **Sharing** → turn on **Remote Login**. Note your Mac
   **username** (the short name, e.g. `navin`).
3. **Install Xcode** from the App Store, then run once in Terminal:
   `xcode-select --install` and `sudo xcodebuild -license accept`.
   Confirm a simulator exists: `xcrun simctl list devices available | head`.
4. **Let WSL SSH in without a password** — on the WSL box I'll generate/print a public key; you add it
   to the Mac's `~/.ssh/authorized_keys` (or run, from the Mac:
   `ssh-copy-id swami@<wsl-tailnet-name>` is the reverse — easier is: paste the WSL public key the
   agent gives you into the Mac's authorized_keys).

## Then tell me (or set it yourself)
Add to `~/projects/agent-os/.env.local`:
```
MAC_HOST=navins-macbook        # the Mac's Tailscale name or 100.x IP
MAC_USER=navin                 # your Mac short username
```
Then I run `mac_runner.py check` — it should report `ssh: True, xcode: ..., simctl_available: True`.

## What you get once connected
- `mac_runner.py sims` — list iOS simulators.
- `mac_runner.py ios-e2e <app.app> <bundle_id>` — boot a simulator, install, launch, screenshot.
- The Controller can dispatch iOS build/test stages to the Mac; screenshots flow back into the QA
  harness + object store for the same a11y/visual/vision-critic treatment as web apps.
- macOS app builds, TestFlight uploads, notarization — all run on the Mac, governed + audited from here.

Everything stays on your hardware (WSL + your Mac) over your private tailnet. No cloud.
