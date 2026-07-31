-- Return to the tab you came from when a diffview closes.
--
-- Closing a tab drops you on the positionally adjacent one, not the one you
-- opened from. Recording the origin at each call site would only cover the
-- openers we wrote -- a typed :DiffviewOpen, a panel key, or a future keymap
-- would all miss it. So instead: track the last tab that was NOT a diffview,
-- continuously, and restore it on diffview's own ViewClosed event. Every close
-- path benefits, <leader>gq included.
--
-- Tabpage HANDLES throughout, never numbers: numbers shift when a tab closes.

local M = {}

local origin_tabpage = nil

local function is_diffview_tab(tabpage)
  -- diffview is lazy-loaded, so the module may legitimately not be there yet
  local loaded, lib = pcall(require, "diffview.lib")
  if loaded then
    for _, view in ipairs(lib.views) do
      if view.tabpage == tabpage then
        return true
      end
    end
  end
  -- lib.views is already empty while a view is being torn down, so fall back to
  -- what the tab is actually displaying
  for _, window in ipairs(vim.api.nvim_tabpage_list_wins(tabpage)) do
    local buffer = vim.api.nvim_win_get_buf(window)
    if vim.api.nvim_buf_get_name(buffer):match("^diffview:") or vim.bo[buffer].filetype:match("^Diffview") then
      return true
    end
  end
  return false
end

local function remember()
  local tabpage = vim.api.nvim_get_current_tabpage()
  if not is_diffview_tab(tabpage) then
    origin_tabpage = tabpage
  end
end

function M.setup()
  local group = vim.api.nvim_create_augroup("diffview_return", { clear = true })

  vim.api.nvim_create_autocmd({ "TabEnter", "WinEnter", "BufWinEnter" }, {
    group = group,
    -- scheduled: on TabEnter the new tab's windows are not populated yet, so an
    -- immediate is_diffview_tab() would call a fresh diffview tab an origin
    callback = vim.schedule_wrap(remember),
  })

  vim.api.nvim_create_autocmd("User", {
    group = group,
    pattern = "DiffviewViewClosed",
    callback = function()
      local target = origin_tabpage
      -- scheduled: the view's tab is still being torn down when the event fires
      vim.schedule(function()
        if target and vim.api.nvim_tabpage_is_valid(target) then
          vim.api.nvim_set_current_tabpage(target)
        else
          pcall(vim.cmd, "tabnext #") -- g<Tab>: last accessed, for views opened before this loaded
        end
      end)
    end,
  })

  remember()
end

return M
