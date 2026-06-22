# dotfiles

Captured host config for the agent-os WSL2/Ubuntu box (user `swami`). Restore with:

```bash
cp dotfiles/tmux.conf   ~/.tmux.conf
cp dotfiles/bashrc      ~/.bashrc
cp dotfiles/profile     ~/.profile
cp dotfiles/gitconfig   ~/.gitconfig
tmux source-file ~/.tmux.conf   # reload tmux without restarting
```

No secrets are stored here (scanned before commit). Real secrets live only in the
gitignored `.env.local` / `postgres/.env`, which are never committed.
