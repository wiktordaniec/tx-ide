-- Options are automatically loaded before lazy.nvim startup
-- Default options that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/options.lua
-- Add any additional options here

-- Pin dark background so tokyonight doesn't auto-detect light mode in tmux sessions
vim.opt.background = "dark"

-- Line wrapping
vim.opt.wrap = true
vim.opt.breakindent = true
vim.opt.linebreak = true
vim.opt.textwidth = 100
