-- Keymaps are automatically loaded on the VeryLazy event
-- Default keymaps that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/keymaps.lua
-- Add any additional keymaps here

-- AINote: leave comments for AI agents to pick up
vim.keymap.set("n", "<leader>an", function()
  local line = vim.api.nvim_win_get_cursor(0)[1]
  local indent = vim.fn.indent(line)
  local prefix = string.rep(" ", indent)
  local cs = vim.bo.commentstring
  local comment_prefix, comment_suffix = cs:match("^(.-)%%s(.*)$")
  if not comment_prefix or comment_prefix == "" then comment_prefix = "# " end
  comment_suffix = comment_suffix or ""
  local lead = "AINote: "
  local text = prefix .. comment_prefix .. lead .. comment_suffix
  vim.api.nvim_buf_set_lines(0, line - 1, line - 1, false, { text })
  vim.api.nvim_win_set_cursor(0, { line, #prefix + #comment_prefix + #lead })
  vim.cmd("startinsert")
end, { desc = "Add AINote above" })

-- Never-pressed LazyVim defaults, removed on the keylog's evidence (0 uses in 15
-- days). These are set by lazyvim/config/keymaps.lua directly rather than through
-- a plugin spec, so a `false` entry in a spec cannot reach them -- they have to be
-- deleted here, which runs after LazyVim's own keymaps. pcall because LazyVim only
-- maps <leader>gG when lazygit is on PATH, and may drop either in a future release.
for _, keymap in ipairs({
  { mode = "n", lhs = "<leader>gG" }, -- Lazygit (cwd)
  { mode = "n", lhs = "<leader>gY" }, -- Git Browse (copy)
  { mode = "x", lhs = "<leader>gY" },
}) do
  pcall(vim.keymap.del, keymap.mode, keymap.lhs)
end

-- Macro trap: q starts a recording whenever the buffer does not map it, which the
-- keylog caught 42 times in one week -- all accidental, one still running, and
-- snacks disables scroll animation while recording, which read as "scrolling
-- broke". Global normal mode only, so buffer-local q (quickfix, help, diffview
-- panels, pickers) still wins and "q to close" keeps working.
vim.keymap.set("n", "q", "<Nop>", { desc = "disabled (use Q to record a macro)" })
vim.keymap.set("n", "Q", "q", { desc = "Record macro" })

-- <leader>aT: the agent's code tour, applied or cleared. Toggles the same way
-- <leader>gF does -- nothing showing, pick a manifest from the directory the
-- agent writes them to; a tour showing, clear its marks and annotations. One
-- manifest is applied without asking, which is the usual case.
vim.keymap.set("n", "<leader>aT", function()
  local tour = require("agent-tour")
  if tour.is_active() then
    tour.clear()
    return vim.notify("tour cleared")
  end
  local manifests = vim.fn.glob(tour.MANIFEST_DIR .. "/*.json", false, true)
  if #manifests == 0 then
    return vim.notify("no tour manifests in " .. tour.MANIFEST_DIR, vim.log.levels.WARN)
  end
  if #manifests == 1 then
    return tour.apply_tour(manifests[1])
  end
  vim.ui.select(manifests, {
    prompt = "Apply tour:",
    format_item = function(path) return vim.fn.fnamemodify(path, ":t:r") end,
  }, function(choice)
    if choice then tour.apply_tour(choice) end
  end)
end, { desc = "Toggle agent code tour" })

-- Diff current buffer against HEAD (inline, single file)
vim.keymap.set("n", "<leader>gd", "<cmd>Gitsigns diffthis<cr>", { desc = "Diff this file against HEAD" })

-- Diff current buffer against PR base / master
vim.keymap.set("n", "<leader>gD", function()
  local out = vim.fn.systemlist("git rev-parse --abbrev-ref origin/HEAD")
  local base = (vim.v.shell_error == 0 and out[1] and out[1] ~= "") and out[1] or "origin/master"
  vim.cmd("Gitsigns diffthis " .. base)
end, { desc = "Diff this file against PR base" })

-- Inline diff vs a picked commit (gitsigns, single pane). <leader>gF toggles:
-- no inline diff active -> pick a commit from this file's history and apply;
-- active -> reset the base back to index/HEAD. b:inline_diff_base tracks the
-- state and feeds the statusline indicator (see plugins/inline-diff-statusline).
-- Multi-select is ignored on purpose -- Tab also moves the cursor.
--
-- This is the most-used git key in the keylog (292 all-time / 124 in the last
-- week), which is why it sits on gF and not behind a new prefix.
local function inline_diff_reset()
  local gitsigns = require("gitsigns")
  gitsigns.change_base(nil, false)
  gitsigns.toggle_linehl(false)
  gitsigns.toggle_deleted(false)
  gitsigns.toggle_word_diff(false)
  vim.b.inline_diff_base = nil
end

local function inline_diff_toggle()
  if vim.b.inline_diff_base then
    inline_diff_reset()
    return
  end
  local buffer = vim.api.nvim_get_current_buf()
  Snacks.picker.git_log_file({
    title = "Inline diff: working tree vs picked commit",
    confirm = function(picker, item)
      picker:close()
      if not (item and item.commit) then
        return
      end
      local gitsigns = require("gitsigns")
      gitsigns.change_base(item.commit, false)
      gitsigns.toggle_linehl(true)
      gitsigns.toggle_deleted(true)
      gitsigns.toggle_word_diff(true)
      vim.b[buffer].inline_diff_base = item.commit:sub(1, 8)
    end,
  })
end

vim.keymap.set("n", "<leader>gF", inline_diff_toggle, { desc = "Toggle inline diff vs picked commit" })

-- Make DiffView buffers modifiable so AINote and other edits work.
-- DiffView locks buffers via multiple code paths, so we defer and hit several events.
local function unlock_diff_buffer()
  local ft = vim.bo.filetype
  if ft == "DiffviewFiles" or ft == "DiffviewFileHistory" then return end
  if vim.wo.diff or vim.bo.buftype == "acwrite" then
    vim.schedule(function()
      if vim.api.nvim_buf_is_valid(0) then
        vim.bo.modifiable = true
        vim.bo.readonly = false
      end
    end)
  end
end

vim.api.nvim_create_autocmd({ "BufEnter", "BufWinEnter", "WinEnter", "FileType" }, {
  pattern = "*",
  callback = unlock_diff_buffer,
})

-- Better diff: word-level matching inside changed lines + more prominent highlight
vim.opt.diffopt:append("linematch:400")
vim.opt.diffopt:append("algorithm:histogram")
vim.opt.diffopt:append("indent-heuristic")

-- GitHub-style diff colors (shared by diff mode, diffview, gitsigns inline)
require("config.diff-style")

-- C-h/j/k/l across splits, panes, and nested sessions (see lua/config/tmux-nav.lua)
require("config.tmux-nav").setup()

-- Closing a diffview lands you back on the tab you opened it from
-- (see lua/config/diffview-return.lua)
require("config.diffview-return").setup()

-- Keymap usage telemetry (see lua/config/keylog.lua)
require("config.keylog").setup()

-- <leader>at / <leader>ac / <leader>aC — ask a live tx llm session about the code
-- under the cursor, one question or a queued batch (see lua/config/tx-ask.lua)
require("config.tx-ask")

-- Agent code tours: registers :TourApply / :TourClear and the autocmd that keeps
-- annotations on screen as buffers come and go (see lua/agent-tour.lua). Nothing
-- else pulls this module in, so without the require even :TourApply is missing.
require("agent-tour")
