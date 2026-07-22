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

-- DiffView: merge-base diff against the PR base (GitHub-style view).
-- Prefers the open PR's base branch via `gh`; falls back to `origin/HEAD`
-- so it still works on branches without a PR or in repos without `gh`.
vim.keymap.set("n", "<leader>gm", function()
  local base
  if vim.fn.executable("gh") == 1 then
    local out = vim.fn.systemlist("gh pr view --json baseRefName --jq .baseRefName 2>/dev/null")
    if vim.v.shell_error == 0 and out[1] and out[1] ~= "" then
      base = "origin/" .. out[1]
    end
  end
  if not base then
    local out = vim.fn.systemlist("git rev-parse --abbrev-ref origin/HEAD")
    if vim.v.shell_error == 0 and out[1] and out[1] ~= "" then
      base = out[1]
    end
  end
  if not base then
    vim.notify("Couldn't resolve diff base — no open PR and origin/HEAD missing", vim.log.levels.ERROR)
    return
  end
  -- Use single rev (not base..HEAD or base...HEAD) so the right pane is the
  -- working tree and stays editable — needed for <leader>an (AINote).
  vim.cmd("DiffviewOpen " .. base)
end, { desc = "Diff branch against PR base (PR-style)" })

-- Diff current buffer against HEAD (inline, single file)
vim.keymap.set("n", "<leader>gd", "<cmd>Gitsigns diffthis<cr>", { desc = "Diff this file against HEAD" })

-- Diff current buffer against PR base / master
vim.keymap.set("n", "<leader>gD", function()
  local out = vim.fn.systemlist("git rev-parse --abbrev-ref origin/HEAD")
  local base = (vim.v.shell_error == 0 and out[1] and out[1] ~= "") and out[1] or "origin/master"
  vim.cmd("Gitsigns diffthis " .. base)
end, { desc = "Diff this file against PR base" })

-- Pick commit(s) from this file's history:
--   Enter on one commit  -> inline diff: working tree vs that commit (gitsigns, single pane)
--   Tab-select two, Enter -> Diffview of the file between those two commits
-- <leader>gR resets the inline view back to normal (base = index/HEAD).
vim.keymap.set("n", "<leader>gF", function()
  local file = vim.api.nvim_buf_get_name(0)
  Snacks.picker.git_log_file({
    title = "1 pick: vs working tree (inline) | Tab x2: commit range",
    confirm = function(picker, item)
      local sel = picker:selected({ fallback = true })
      picker:close()
      if #sel >= 2 then
        -- idx 1 = newest; sort so older commit is the left/base side
        table.sort(sel, function(a, b) return (a.idx or 0) > (b.idx or 0) end)
        vim.cmd(("DiffviewOpen %s..%s -- %s"):format(sel[1].commit, sel[2].commit, vim.fn.fnameescape(file)))
      elseif sel[1] and sel[1].commit then
        local gs = require("gitsigns")
        gs.change_base(sel[1].commit, false)
        gs.toggle_linehl(true)
        gs.toggle_deleted(true)
        gs.toggle_word_diff(true)
      end
    end,
  })
end, { desc = "Inline diff vs picked commit / range between two picks" })

vim.keymap.set("n", "<leader>gR", function()
  local gs = require("gitsigns")
  gs.change_base(nil, false)
  gs.toggle_linehl(false)
  gs.toggle_deleted(false)
  gs.toggle_word_diff(false)
end, { desc = "Reset inline diff (base back to HEAD, toggles off)" })

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

-- Seamless C-h/j/k/l across splits, tmux panes, and nested sessions (see lua/config/tmux-nav.lua)
require("config.tmux-nav").setup()

-- Keymap usage telemetry (see lua/config/keylog.lua)
require("config.keylog").setup()
