-- Inline-review mode for diffview: one review surface, two render modes.
--
--   side   : diffview's native two-window side-by-side diff
--   inline : ONE window with the full working-tree file -- editable, LSP alive,
--            the diff rendered by gitsigns against the view's base rev
--            (linehl + word_diff + deleted virtual lines, ]h/[h hunk jumps)
--
-- <leader>ai flips the current file between the modes in place, inside the
-- diffview tab -- in the agent-review group next to <leader>ad/<leader>aD,
-- which open the review diffview this mode operates on. The trick that keeps everything else native: the mode is not
-- state held on the side -- it IS the layout class of the view's file entries.
-- Side mode entries hold diffview's stock two-window Diff2 layout; inline mode
-- converts them to the single-window Diff1 layout through diffview's own
-- FileEntry:convert_layout + view:set_file, the exact mechanism behind the
-- built-in cycle_layout action (g<C-x>). Because every open route re-uses the
-- entry's layout, everything is mode-preserving for free:
--   * <Tab>/<S-Tab> -- diffview's buffer-local select_next/prev_entry, live in
--     both the diff buffers and the panel (no global <Tab> map, so the
--     jumplist's <C-i> is untouched outside the view)
--   * the file panel's <CR> (select_entry), panel cursor-follow included
--   * git-watcher refreshes: entries survive updates by identity, so saving an
--     edit made in inline mode does not knock the view back to side mode
-- No window surgery, no DiffviewRefresh healing.
--
-- Diff1's single window shows the b-side File -- the same real working-tree
-- buffer diffview already uses for the right-hand side window -- so edits and
-- LSP just work. That File carries diff2 window options (diff mode, scrollbind,
-- diff folds); shown alone they would leave a windowless diff with everything
-- folded, so inline mode swaps them out and restores the originals on the flip
-- back. gitsigns gets change_base(<the view's left rev>) per buffer as files
-- open (file_open_post), and the render toggles -- GLOBAL in gitsigns -- follow
-- the mode.
--
-- Status handling:
--   M : gitsigns hunks as usual
--   A : no base blob, so gitsigns has nothing to hunk against and renders
--       nothing -- the whole file is washed green (house GitSignsAddLn palette)
--       and tagged NEW FILE in the statusline
--   R : gitsigns diffs base:<new path>, which does not exist in the base, so
--       hunks are unavailable -- tagged "renamed from <oldpath>" in the
--       statusline; side mode shows the true rename diff
--   D : no working-tree file to show -- D entries keep the side-by-side
--       layout (removed content vs empty) even while the view is inline
--
-- The base rev is derived from the open view (view.left), never hardcoded, and
-- feeds the " vs <base>" statusline indicator via b:inline_diff_base (see
-- plugins/inline-diff-statusline.lua). Inline mode requires the view's right
-- side to be the working tree (:DiffviewOpen <base>); rev-range views
-- (<base>...HEAD) have no working-tree side and stay side-by-side.

local added_namespace = vim.api.nvim_create_namespace("inline_review_added")

-- Buffers that received a gitsigns base / statusline vars / green wash, so the
-- flip back to side mode (or the view closing) can unwind them.
local touched_buffers = {}

-- b-side Files whose window options were swapped for inline display, mapped to
-- the original table for the flip back. Weak keys: Files die with their view.
local saved_winopts = setmetatable({}, { __mode = "k" })

-- Views whose emitter already carries this module's listeners. Same lifetime
-- argument as above.
local hooked_views = setmetatable({}, { __mode = "k" })

-- Monotonic id for the focus/cursor restore of the most recent flip. A flip
-- issued while the previous one's async open is still in flight leaves the
-- older restore hook pending; matching against this drops it instead of
-- letting it fire mid-transition with stale focus state.
local restore_generation = 0

local function inline_layout_class()
  return require("diffview.scene.layouts.diff_1").Diff1
end

local function side_layout_class()
  if require("diffview.config").get_config().view.default.layout == "diff2_vertical" then
    return require("diffview.scene.layouts.diff_2_ver").Diff2Ver
  end
  return require("diffview.scene.layouts.diff_2_hor").Diff2Hor
end

local function find_review_view()
  local diffview_lib = require("diffview.lib")
  local DiffView = require("diffview.scene.views.diff.diff_view").DiffView
  local view = diffview_lib.get_current_view()
  if view and view:instanceof(DiffView) then
    return view
  end
  for _, candidate in ipairs(diffview_lib.views) do
    if candidate:instanceof(DiffView) then
      return candidate
    end
  end
end

-- The entries inline mode can convert: their b side must be the real working
-- tree, and actually exist there. Excludes the staged section (b is an index
-- blob), rev-range views (b is a commit blob), merge conflicts (owned by the
-- merge tool), and Deleted files (b is nulled -- inline would show a blank
-- diffview://null buffer, while side-by-side shows the removed content, so D
-- entries keep their native layout in both modes).
local function working_tree_entries(view)
  local RevType = require("diffview.vcs.rev").RevType
  local entries = {}
  for _, entry in ipairs(view.panel:ordered_file_list() or {}) do
    local window_b = entry.layout.b
    if
      entry.kind ~= "conflicting"
      and window_b
      and window_b.file.rev.type == RevType.LOCAL
      and not window_b.file.nulled
    then
      entries[#entries + 1] = entry
    end
  end
  return entries
end

-- Derived from the entries' actual layout class, never stored: a stored
-- boolean drifts the moment diffview rebuilds entries under us.
local function view_is_inline(view)
  local entry = working_tree_entries(view)[1]
  return entry ~= nil and entry.layout:instanceof(inline_layout_class())
end

-- Base rev, derived from the open view. The sha feeds gitsigns; the label (the
-- typed rev argument when there is one, e.g. "develop") feeds the statusline.
local function view_base(view)
  local sha = view.left.commit
  return sha, view.rev_arg or (sha and sha:sub(1, 8)) or "?"
end

-- gitsigns' render toggles are global. The current value lives in gitsigns'
-- own config -- mirroring it in a local boolean is how it drifts.
local function render_inline_diff(on)
  local gitsigns = require("gitsigns")
  local gitsigns_config = require("gitsigns.config").config
  if gitsigns_config.linehl ~= on then
    gitsigns.toggle_linehl(on)
  end
  if gitsigns_config.show_deleted ~= on then
    gitsigns.toggle_deleted(on)
  end
  if gitsigns_config.word_diff ~= on then
    gitsigns.toggle_word_diff(on)
  end
end

-- Window options for the single inline window. Fold options come from the
-- global values so the window behaves like any freshly opened file.
local function inline_winopts()
  return {
    diff = false,
    scrollbind = false,
    cursorbind = false,
    foldmethod = vim.o.foldmethod,
    foldenable = vim.o.foldenable,
    foldlevel = vim.o.foldlevel,
    foldcolumn = vim.o.foldcolumn,
  }
end

local function convert_entries(view, inline)
  local target_layout = inline and inline_layout_class() or side_layout_class()
  for _, entry in ipairs(working_tree_entries(view)) do
    if not entry.layout:instanceof(target_layout) then
      local file_b = entry.layout.b.file
      if inline then
        saved_winopts[file_b] = file_b.winopts
        file_b.winopts = inline_winopts()
      elseif saved_winopts[file_b] then
        file_b.winopts = saved_winopts[file_b]
        saved_winopts[file_b] = nil
      end
      entry:convert_layout(target_layout)
    end
  end
end

-- Whole-file green wash for files Added on the branch. Painted over the lines
-- present at open; lines typed afterwards stay unwashed, which is fine -- the
-- wash and the tag only signpost "everything here is new".
local function paint_added(buffer)
  vim.api.nvim_buf_clear_namespace(buffer, added_namespace, 0, -1)
  for line = 0, vim.api.nvim_buf_line_count(buffer) - 1 do
    vim.api.nvim_buf_set_extmark(buffer, added_namespace, line, 0, {
      line_hl_group = "GitSignsAddLn",
    })
  end
end

-- gitsigns' attach is async and throttled per buffer: when its own autocmds
-- already started one (they race us on freshly loaded buffers), an attach()
-- call returns before the buffer is actually attached -- and change_base on an
-- unattached buffer is a silent no-op. The one reliable readiness signal is
-- the cache entry change_base itself consults, so kick an attach and poll for
-- the entry (bounded; an un-attachable buffer simply never gets a base).
local function apply_base(buffer, base_sha, attempts)
  if not vim.api.nvim_buf_is_valid(buffer) then
    return
  end
  if require("gitsigns.cache").cache[buffer] then
    -- scheduled: this can run inside diffview's file_open_post continuation,
    -- where textlock forbids nvim_buf_call. change_base only acts on the
    -- current buffer, hence the buf_call.
    vim.schedule(function()
      if vim.api.nvim_buf_is_valid(buffer) then
        vim.api.nvim_buf_call(buffer, function()
          require("gitsigns").change_base(base_sha, false)
        end)
      end
    end)
    return
  end
  if attempts > 0 then
    vim.defer_fn(function()
      apply_base(buffer, base_sha, attempts - 1)
    end, 200)
  end
end

-- Runs on every file_open_post of an inline entry, so a file opened by any
-- route (panel <CR>, <Tab>, <leader>ai) gets the base applied. Idempotent.
local function decorate_inline_buffer(view, entry)
  local buffer = entry.layout.b.file.bufnr
  if not (buffer and vim.api.nvim_buf_is_loaded(buffer)) then
    return
  end
  local base_sha, base_label = view_base(view)

  touched_buffers[buffer] = true
  vim.b[buffer].inline_diff_base = base_label

  local tag
  if entry.status == "A" then
    tag = "NEW FILE"
    paint_added(buffer)
  elseif entry.status == "R" then
    tag = "renamed from " .. (entry.oldpath or "?")
  end
  if tag and vim.b[buffer].inline_review_tag ~= tag then
    vim.notify(("%s (%s)"):format(entry.path, tag))
  end
  vim.b[buffer].inline_review_tag = tag

  require("gitsigns").attach({ bufnr = buffer })
  apply_base(buffer, base_sha, 50)
end

local function unwind_buffers()
  local gitsigns = require("gitsigns")
  for buffer in pairs(touched_buffers) do
    if vim.api.nvim_buf_is_valid(buffer) then
      vim.api.nvim_buf_clear_namespace(buffer, added_namespace, 0, -1)
      vim.b[buffer].inline_diff_base = nil
      vim.b[buffer].inline_review_tag = nil
      vim.api.nvim_buf_call(buffer, function()
        gitsigns.change_base(nil, false)
      end)
    end
  end
  touched_buffers = {}
end

local function hook_view(view)
  if hooked_views[view] then
    return
  end
  hooked_views[view] = true

  view.emitter:on("file_open_post", function(_, entry)
    if entry.kind ~= "conflicting" and entry.layout:instanceof(inline_layout_class()) then
      decorate_inline_buffer(view, entry)
    end
  end)

  -- A git-watcher refresh keeps existing entries by identity but creates brand
  -- new entries with the stock side layout; fold those into the active mode.
  view.emitter:on("files_updated", function()
    if view_is_inline(view) then
      convert_entries(view, true)
    end
  end)
end

-- Flip the whole view and re-open one entry with the new layout. Cursor and
-- focus restoration ride the layout's files_opened event because set_file is
-- async -- the same pattern as diffview's cycle_layout.
local function set_mode(view, inline, entry_to_open)
  hook_view(view)

  if inline and #working_tree_entries(view) == 0 then
    return vim.notify(
      "inline review needs a working-tree diffview (:DiffviewOpen <base>)",
      vim.log.levels.WARN
    )
  end

  local entry = entry_to_open or view.cur_entry
  local was_focused = view.cur_layout:is_focused()
  local origin_window = vim.api.nvim_get_current_win()
  local origin_was_panel = view.panel.winid == origin_window
  local cursor
  if entry and entry == view.cur_entry then
    local main_window = view.cur_layout:get_main_win()
    if main_window and vim.api.nvim_win_is_valid(main_window.id) then
      cursor = vim.api.nvim_win_get_cursor(main_window.id)
    end
  end

  convert_entries(view, inline)
  render_inline_diff(inline)
  if not inline then
    unwind_buffers()
  end

  if not entry or entry.kind == "conflicting" then
    return
  end

  -- Force StandardView.use_entry down its fresh-layout branch: re-using the
  -- view's cached layout for the target class crashes diffview's open_files
  -- on nil window ids (the cache holds a layout whose windows were destroyed
  -- by the previous swap), which aborts the open mid-flight.
  view.layouts[inline and inline_layout_class() or side_layout_class()] = nil

  -- file_open_post, not the layout's files_opened: files_opened can fire an
  -- extra time mid-rebuild, before the final windows exist. file_open_post is
  -- emitted exactly once, after the open has fully settled.
  restore_generation = restore_generation + 1
  local generation = restore_generation
  view.emitter:once("file_open_post", function()
    -- scheduled: the event is emitted under textlock, where window switching
    -- is forbidden
    vim.schedule(function()
      if generation ~= restore_generation then
        return
      end
      local main_window = view.cur_layout:get_main_win()
      if cursor then
        pcall(vim.api.nvim_win_set_cursor, main_window.id, cursor)
      end
      -- the cross-class window rebuild drops focus onto the new file window; a
      -- flip issued from the panel should leave the panel focused, like the
      -- panel's own <CR> does. The rebuild also closes and re-opens the panel
      -- window itself, so the panel is refound rather than restored by id.
      if was_focused then
        main_window:focus()
      elseif origin_was_panel and view.panel:is_open() then
        view.panel:focus()
      elseif vim.api.nvim_win_is_valid(origin_window) then
        vim.api.nvim_set_current_win(origin_window)
      end
    end)
  end)
  view:set_file(entry, false, true)
end

local function flip_mode()
  local view = find_review_view()
  if not view then
    return vim.notify("no diffview open (:DiffviewOpen <base>)", vim.log.levels.WARN)
  end
  if vim.api.nvim_get_current_tabpage() ~= view.tabpage then
    vim.api.nvim_set_current_tabpage(view.tabpage)
  end

  local entry_to_open
  if vim.bo.filetype == "DiffviewFiles" then
    local item = view.panel:get_item_at_cursor()
    -- directories in the tree listing carry no layout
    if item and item.layout then
      entry_to_open = item
    end
  end
  set_mode(view, not view_is_inline(view), entry_to_open)
end

local function panel_open_inline()
  local view = find_review_view()
  if not view then
    return
  end
  local item = view.panel:get_item_at_cursor()
  if item and item.layout then
    set_mode(view, true, item)
  end
end

-- The render toggles and per-buffer bases must not outlive the view,
-- whichever way it closes (<leader>gq, :DiffviewClose, :tabclose).
vim.api.nvim_create_autocmd("User", {
  group = vim.api.nvim_create_augroup("inline_review", { clear = true }),
  pattern = "DiffviewViewClosed",
  callback = function()
    if next(touched_buffers) then
      render_inline_diff(false)
      unwind_buffers()
    end
  end,
})

return {
  {
    "sindrets/diffview.nvim",
    opts = {
      -- the review surface's single file list: diffview's own panel, docked at
      -- the bottom and identical in both modes
      file_panel = {
        win_config = { position = "bottom", height = 12 },
      },
      keymaps = {
        file_panel = {
          -- shadows diffview's default i (toggle list/tree listing style)
          { "n", "i", panel_open_inline, { desc = "Open the file inline (gitsigns vs base)" } },
        },
      },
    },
    keys = {
      { "<leader>ai", flip_mode, desc = "Flip inline <-> side-by-side (diffview)" },
    },
  },

  -- Statusline tag for the inline special cases (NEW FILE / renamed). Sits next
  -- to the " vs <base>" indicator from plugins/inline-diff-statusline.lua,
  -- which inline mode feeds through the same b:inline_diff_base.
  {
    "nvim-lualine/lualine.nvim",
    opts = function(_, opts)
      table.insert(opts.sections.lualine_c, {
        function()
          return vim.b.inline_review_tag or ""
        end,
        color = { fg = "#c3e88d", gui = "bold" },
      })
      return opts
    end,
  },
}
