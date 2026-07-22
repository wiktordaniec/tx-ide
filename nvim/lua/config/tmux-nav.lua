-- <C-h/j/k/l>: nvim splits first, then tmux panes — the nvim half of tx-ide's
-- @tx-ide-nav-keys (tmux/tx-ide.tmux). At the tabpage edge, hand off to bin/tmux-nav,
-- which walks out of tx's nested client-in-a-pane sessions.

local M = {}

-- Resolve bin/tmux-nav THROUGH the ~/.config/nvim symlink (plain dirname math on the
-- symlinked path would escape into ~/.config); $PATH fallback outside a checkout.
local source_file = debug.getinfo(1, "S").source:sub(2)
local navigator = vim.uv.fs_realpath(vim.fs.dirname(source_file) .. "/../../../bin/tmux-nav")
  or "tmux-nav"

local directions = {
  h = { flag = "L", name = "left" },
  j = { flag = "D", name = "lower" },
  k = { flag = "U", name = "upper" },
  l = { flag = "R", name = "right" },
}

local function navigate(key)
  local window = vim.api.nvim_get_current_win()
  vim.cmd.wincmd(key)
  if vim.api.nvim_get_current_win() ~= window then
    return
  end
  if vim.env.TMUX then
    vim.system({ navigator, directions[key].flag, vim.env.TMUX_PANE })
  end
end

function M.setup()
  for key, direction in pairs(directions) do
    vim.keymap.set({ "n", "t" }, "<C-" .. key .. ">", function()
      navigate(key)
    end, { desc = "Go to " .. direction.name .. " window/pane" })
  end
end

return M
