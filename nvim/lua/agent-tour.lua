-- agent-tour: walk the user through code via uppercase global marks, line
-- highlights, and inline annotations rendered above each marked line.
-- Driven by a JSON manifest written by the agent before spawning nvim:
--   {
--     "cwd": "/path/to/worktree",
--     "marks": [
--       {
--         "file": "src/foo.py",
--         "line": 42,
--         "mark": "A",
--         "head": "[A] race surface",
--         "body": [
--           "Three tasks waited on simultaneously…",
--           "Asyncio scheduler picks the winner."
--         ]
--       }
--     ]
--   }
-- Spawn pattern (from DEVELOPER.md):
--   nvim -c "lua require('agent-tour').apply_tour('/tmp/claude-tour/<session>.json')"

local M = {}

-- where the agent writes its manifests, per the spawn pattern above
M.MANIFEST_DIR = "/tmp/claude-tour"

local namespace = vim.api.nvim_create_namespace("agent-tour")
local marks_by_path = {}

vim.api.nvim_set_hl(0, "TourMark", { link = "Visual", default = true })
vim.api.nvim_set_hl(0, "TourNoteHead", { link = "WarningMsg", default = true })
vim.api.nvim_set_hl(0, "TourNote", { link = "Comment", default = true })

local function apply_mark(buffer, entry)
  vim.api.nvim_buf_set_extmark(buffer, namespace, entry.line - 1, 0, {
    line_hl_group = "TourMark",
  })
  if entry.head or entry.body then
    local virt_lines = {}
    if entry.head then
      table.insert(virt_lines, { { entry.head, "TourNoteHead" } })
    end
    for _, line in ipairs(entry.body or {}) do
      table.insert(virt_lines, { { "  " .. line, "TourNote" } })
    end
    vim.api.nvim_buf_set_extmark(buffer, namespace, entry.line - 1, 0, {
      virt_lines = virt_lines,
      virt_lines_above = true,
    })
  end
end

local function apply_marks_for_buffer(buffer, path)
  local entries = marks_by_path[path]
  if not entries then
    return
  end
  vim.api.nvim_buf_clear_namespace(buffer, namespace, 0, -1)
  for _, entry in ipairs(entries) do
    apply_mark(buffer, entry)
  end
end

local function read_manifest(path)
  local file = io.open(path, "r")
  if not file then
    return nil, "manifest not found: " .. path
  end
  local content = file:read("*a")
  file:close()
  local ok, manifest = pcall(vim.fn.json_decode, content)
  if not ok or type(manifest) ~= "table" then
    return nil, "invalid JSON in manifest: " .. path
  end
  return manifest, nil
end

function M.apply_tour(manifest_path)
  local manifest, error_message = read_manifest(manifest_path)
  if not manifest then
    vim.notify(error_message, vim.log.levels.ERROR)
    return
  end

  if manifest.cwd then
    vim.cmd("cd " .. vim.fn.fnameescape(manifest.cwd))
  end

  marks_by_path = {}

  -- A stop is routinely a file the operator already has open in another nvim,
  -- whose swapfile makes :edit print W325 -- once per stop, mid-apply. shortmess
  -- "A" is the only thing that silences it: :noswapfile suppresses *creating* a
  -- swapfile, not the check against an existing one. Restored below, because
  -- this is the operator's editor and the flag would otherwise also hide the
  -- prompt for their own edits.
  local shortmess = vim.o.shortmess
  vim.o.shortmess = shortmess .. "A"

  for _, entry in ipairs(manifest.marks or {}) do
    vim.cmd("edit " .. vim.fn.fnameescape(entry.file))
    local buffer = vim.api.nvim_get_current_buf()
    vim.api.nvim_buf_set_mark(buffer, entry.mark, entry.line, 0, {})

    local absolute_path = vim.fn.fnamemodify(entry.file, ":p")
    marks_by_path[absolute_path] = marks_by_path[absolute_path] or {}
    table.insert(marks_by_path[absolute_path], entry)
  end

  vim.o.shortmess = shortmess

  for path, _ in pairs(marks_by_path) do
    for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_loaded(buffer) and vim.api.nvim_buf_get_name(buffer) == path then
        apply_marks_for_buffer(buffer, path)
      end
    end
  end

  pcall(vim.cmd, "normal! `A")
end

-- A tour is applied for the length of a session, so it needs a way out: the
-- annotations are extmarks in our own namespace and the jump points are global
-- marks, and both outlive the buffer they were set from.
function M.is_active()
  return next(marks_by_path) ~= nil
end

function M.clear()
  for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buffer) then
      vim.api.nvim_buf_clear_namespace(buffer, namespace, 0, -1)
    end
  end
  for _, entries in pairs(marks_by_path) do
    for _, entry in ipairs(entries) do
      pcall(vim.api.nvim_del_mark, entry.mark)
    end
  end
  marks_by_path = {}
end

vim.api.nvim_create_user_command("TourApply", function(opts)
  M.apply_tour(opts.args)
end, { nargs = 1, complete = "file" })

vim.api.nvim_create_user_command("TourClear", function()
  M.clear()
end, {})

vim.api.nvim_create_autocmd({ "BufWinEnter", "BufReadPost" }, {
  callback = function(args)
    apply_marks_for_buffer(args.buf, vim.api.nvim_buf_get_name(args.buf))
  end,
})

return M
