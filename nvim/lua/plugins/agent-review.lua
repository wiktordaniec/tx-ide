-- <leader>gf: history of the function/class the cursor sits in, as a diffview
-- file-history log. Treesitter finds the enclosing definition, git is then asked
-- for that line range.
--
-- The LINE RANGE form (-L<start>,<end>:<file>) and deliberately NOT git's
-- -L:<funcname>:<file>: the latter resolves the name through git's funcname regex
-- and dies with `fatal: -L parameter '<name>' ... no match` on anything that is
-- not a def/class line, which is most of a real file. Hit in live use.
local function enclosing_definition()
  -- get_node() throws on a buffer with no treesitter parser
  local parsed, node = pcall(vim.treesitter.get_node)
  if not parsed then
    return nil
  end
  while node do
    local kind = node:type()
    if
      kind:match("function_definition")
      or kind:match("class_definition")
      or kind:match("method")
      or kind == "function_declaration"
    then
      local start_row, _, end_row, _ = node:range()
      local name_node = node:field("name")[1]
      local name = name_node and vim.treesitter.get_node_text(name_node, 0) or "<anonymous>"
      return name, start_row + 1, end_row + 1, kind
    end
    node = node:parent()
  end
end

-- git wants the path relative to the repo root, not to the cwd
local function repo_relative_path()
  local absolute = vim.fn.expand("%:p")
  if absolute == "" or vim.bo.buftype ~= "" or absolute:match("^diffview:") then
    return nil
  end
  local root = vim.fn.systemlist({ "git", "-C", vim.fn.expand("%:p:h"), "rev-parse", "--show-toplevel" })[1]
  if vim.v.shell_error ~= 0 or not root or root == "" then
    return nil
  end
  return absolute:sub(#root + 2)
end

local function function_history()
  local path = repo_relative_path()
  if not path then
    return vim.notify("not a tracked file buffer", vim.log.levels.WARN)
  end
  local name, start_row, end_row, kind = enclosing_definition()
  if not name then
    return vim.notify("no enclosing function/class at the cursor", vim.log.levels.WARN)
  end
  vim.notify(("history of %s (%s, lines %d-%d)"):format(name, kind, start_row, end_row))
  vim.cmd(("DiffviewFileHistory -L%d,%d:%s"):format(start_row, end_row, path))
end

return {
  -- which-key group labels
  {
    "folke/which-key.nvim",
    opts = {
      spec = {
        { "<leader>a", group = "agent review" },
      },
    },
  },

  -- AINote keyword for todo-comments
  {
    "folke/todo-comments.nvim",
    opts = {
      keywords = {
        AINOTE = {
          icon = " ",
          color = "info",
          alt = { "AINote" },
        },
      },
    },
  },

  -- Diffview keymaps
  {
    "sindrets/diffview.nvim",
    cmd = { "DiffviewOpen", "DiffviewFileHistory", "DiffviewClose" },
    opts = {
      enhanced_diff_hl = true, -- proper add/delete/change colors instead of generic blue
    },
    keys = {
      { "<leader>gf", function_history, desc = "File history (enclosing function/class)" },
      { "<leader>gv", "<cmd>DiffviewFileHistory %<cr>", desc = "File history (current file)" },
      { "<leader>gH", "<cmd>DiffviewFileHistory<cr>", desc = "File history (branch)" },
      -- the return to the origin tab is an autocmd, not this key: see
      -- lua/config/diffview-return.lua
      { "<leader>gq", "<cmd>DiffviewClose<cr>", desc = "Close diffview (returns to origin tab)" },
      {
        "<leader>ad",
        function()
          local base = vim.fn.system("git merge-base origin/main HEAD"):gsub("%s+", "")
          if vim.v.shell_error ~= 0 then
            vim.notify("Could not find merge-base with origin/main", vim.log.levels.WARN)
            return
          end
          vim.cmd("DiffviewOpen " .. base .. "...HEAD")
        end,
        desc = "Diff vs main",
      },
      {
        "<leader>aD",
        function()
          Snacks.picker.git_log({
            confirm = function(picker, item)
              picker:close()
              if item and item.commit then
                vim.cmd("DiffviewOpen " .. item.commit .. "...HEAD")
              end
            end,
          })
        end,
        desc = "Diff from commit...",
      },
    },
  },

  -- Worktree picker + git status/log under <leader>a
  {
    "folke/snacks.nvim",
    keys = {
      {
        "<leader>aw",
        function()
          local result = vim.fn.system("git worktree list --porcelain")
          local worktrees = {}
          local current = {}

          for line in result:gmatch("[^\n]+") do
            if line:match("^worktree ") then
              if current.path then
                table.insert(worktrees, current)
              end
              current = { path = line:match("^worktree (.+)") }
            elseif line:match("^HEAD ") then
              current.head = line:match("^HEAD (.+)")
            elseif line:match("^branch ") then
              current.branch = line:match("^branch refs/heads/(.+)")
            elseif line:match("^bare$") then
              current.bare = true
            end
          end
          if current.path then
            table.insert(worktrees, current)
          end

          local cwd = vim.uv.cwd()
          local items = {}
          for _, wt in ipairs(worktrees) do
            if not wt.bare then
              items[#items + 1] = {
                text = (wt.branch or "detached") .. " " .. wt.path,
                branch = wt.branch or "detached",
                path = wt.path,
                head = wt.head,
                current = wt.path == cwd,
              }
            end
          end

          Snacks.picker({
            title = "Git Worktrees",
            items = items,
            format = function(item)
              local icon = item.current and " " or " "
              local hl = item.current and "DiagnosticOk" or "DiagnosticInfo"
              return {
                { icon, hl },
                { item.branch, "Title" },
                { "  " },
                { vim.fn.fnamemodify(item.path, ":~"), "Comment" },
              }
            end,
            preview = function(ctx)
              local output = vim.fn.system(
                "git -C " .. vim.fn.shellescape(ctx.item.path) .. " log --oneline --decorate -20"
              )
              ctx:set_text(vim.split(output, "\n"))
            end,
            confirm = function(picker, item)
              picker:close()
              if not item then
                return
              end
              vim.cmd("cd " .. vim.fn.fnameescape(item.path))
              vim.notify("Switched to worktree: " .. item.branch, vim.log.levels.INFO)
            end,
          })
        end,
        desc = "Worktree picker",
      },
    },
  },
}
