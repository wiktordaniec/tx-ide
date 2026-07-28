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
      { "<leader>af", "<cmd>DiffviewFileHistory<cr>", desc = "File history (all)" },
      { "<leader>ah", "<cmd>DiffviewFileHistory %<cr>", desc = "File history (current file)" },
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
      { "<leader>ai", "<cmd>Gitsigns diffthis<cr>", desc = "Inline diff (current file)" },
      { "<leader>am", "<cmd>Gitsigns diffthis main<cr>", desc = "Inline diff vs main" },
      { "<leader>as", function() Snacks.picker.git_status() end, desc = "Git status" },
      { "<leader>al", function() Snacks.picker.git_log() end, desc = "Git log" },
    },
  },
}
