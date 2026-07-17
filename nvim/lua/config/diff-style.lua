-- GitHub-style diff colors, tuned for tokyonight (night/moon).
-- One palette shared by:
--   * vim diff mode / diffview.nvim side-by-side panes (<leader>gm, <leader>ad, 2-pick <leader>gF)
--   * gitsigns single-pane inline diff (<leader>gF: linehl + word_diff + deleted virt lines)
--
-- Design: soft ~15-20% tinted line backgrounds (GitHub's rgba(46,160,67,.15) /
-- rgba(248,81,73,.1) blended onto the tokyonight bg), stronger ~40% tints for
-- intra-line word/char emphasis, fg left untouched so syntax highlighting shows
-- through, and deleted-line filler drawn as dim diagonal slashes instead of a
-- solid red block.

local p = {
  add_line = "#1f3a2f", -- soft green, added line
  add_word = "#2f6b4f", -- brighter green, added chars within a line
  del_line = "#3a2632", -- soft red, deleted line
  del_word = "#7a3441", -- brighter red, deleted chars within a line
  change_line = "#252b41", -- subtle blue-grey, changed line (diff mode)
  change_word = "#3e5380", -- brighter blue, changed chars (DiffText)
  filler_fg = "#3b4261", -- dim slashes for alignment filler (tokyonight fg_gutter)
  del_lnum = "#914c54", -- muted red for virtual-line numbers
}

local function hl(name, spec)
  vim.api.nvim_set_hl(0, name, spec)
end

local function apply()
  -- Core vim diff groups: diff mode, diffview panes (enhanced_diff_hl remaps
  -- per-window but these remain the base look).
  hl("DiffAdd", { bg = p.add_line })
  hl("DiffChange", { bg = p.change_line })
  hl("DiffText", { bg = p.change_word })
  -- Filler for missing lines: no solid block; dim '╱' slashes (see fillchars).
  hl("DiffDelete", { fg = p.filler_fg, bg = "NONE" })

  -- diffview.nvim extras. DiffviewDiffAddAsDelete is the "line removed on this
  -- side" block in the left pane; DiffviewDiffDeleteDim is the filler link
  -- target used when enhanced_diff_hl is on. Set after diffview's own
  -- update_diff_hl() so our values win.
  hl("DiffviewDiffDeleteDim", { fg = p.filler_fg, bg = "NONE" })
  hl("DiffviewDiffDelete", { fg = p.filler_fg, bg = "NONE" })
  hl("DiffviewDiffAddAsDelete", { bg = p.del_line })

  -- gitsigns inline diff (single pane, GitHub "unified" style):
  -- current-buffer lines are the *new* side, so both added and changed lines
  -- read green; the old text arrives as red virtual lines above.
  hl("GitSignsAddLn", { bg = p.add_line })
  hl("GitSignsChangeLn", { bg = p.add_line })
  hl("GitSignsAddLnInline", { bg = p.add_word })
  hl("GitSignsChangeLnInline", { bg = p.add_word })
  hl("GitSignsDeleteLnInline", { bg = p.del_word })
  -- Deleted lines shown as virtual lines (toggle_deleted / preview_hunk_inline)
  hl("GitSignsDeleteVirtLn", { bg = p.del_line })
  hl("GitSignsDeleteVirtLnInLine", { bg = p.del_word })
  hl("GitSignsVirtLnum", { fg = p.del_lnum, bg = p.del_line })
  -- Hunk previews reuse the same palette
  hl("GitSignsAddPreview", { bg = p.add_line })
  hl("GitSignsDeletePreview", { bg = p.del_line })
  hl("GitSignsAddInline", { bg = p.add_word })
  hl("GitSignsChangeInline", { bg = p.add_word })
  hl("GitSignsDeleteInline", { bg = p.del_word })
end

-- Diagonal-slash filler for alignment lines in side-by-side diffs
vim.opt.fillchars:append({ diff = "╱" })

-- diffview re-derives DiffviewDiffAddAsDelete/DiffviewDiffDelete from
-- DiffDelete in its (deferred) init and in its own ColorScheme autocmd, which
-- would clobber the overrides above. Re-apply *scheduled* (after all autocmd
-- callbacks of the event) and again when a view actually opens.
local function apply_later()
  apply()
  vim.schedule(apply)
end

local group = vim.api.nvim_create_augroup("diff_style", { clear = true })
vim.api.nvim_create_autocmd("ColorScheme", { group = group, callback = apply_later })
vim.api.nvim_create_autocmd("User", {
  group = group,
  pattern = { "LazyLoad", "DiffviewViewOpened" },
  callback = function(ev)
    if ev.match == "DiffviewViewOpened" or ev.data == "diffview.nvim" then
      apply_later()
    end
  end,
})
apply()
