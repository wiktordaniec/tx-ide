return {
  { "sindrets/diffview.nvim" },
  {
    "folke/snacks.nvim",
    opts = {
      lazygit = {
        win = {
          width = 0,
          height = 0,
        },
      },
    },
  },
  {
    "polarmutex/git-worktree.nvim",
    version = "^2",
    dependencies = { "nvim-telescope/telescope.nvim", "nvim-lua/plenary.nvim" },
    config = function()
      require("telescope").load_extension("git_worktree")
    end,
    keys = {
      {
        "<leader>aw",
        function()
          require("telescope").extensions.git_worktree.git_worktree()
        end,
        desc = "Switch git worktree",
      },
    },
  },
}
