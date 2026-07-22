-- Seamless <C-h/j/k/l> between nvim splits and tmux panes (the nvim half of tx-ide's
-- @tx-ide-nav-keys; see tmux/tx-ide.tmux). The tmux bind forwards C-h/j/k/l into any pane
-- running nvim; here they move between splits, and when `wincmd` hits the tabpage edge the
-- key hands off to bin/tmux-nav, which selects the neighboring tmux pane — walking OUT of
-- tx's nested client-in-a-pane sessions when this nvim runs inside one.

local M = {}

-- bin/tmux-nav, resolved relative to this file THROUGH the ~/.config/nvim symlink into the
-- repo checkout (plain dirname math on the symlinked path would escape into ~/.config).
-- Outside a repo checkout (this file copied elsewhere), fall back to $PATH.
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
