return {
  {
    "sindrets/diffview.nvim",
    -- The file panel opens full-width at the bottom instead of diffview's stock
    -- 35-col left sidebar: the diff splits keep the full terminal width, and the
    -- file list reads as a list rather than a column of truncated paths. This is
    -- how the `-diff` companions are actually read.
    --
    -- Position is the whole change — diffview's own <leader>b (toggle the panel)
    -- and <leader>e (focus it) are already buffer-local and already nowait, so
    -- they pick up the new position with nothing to override here.
    opts = { file_panel = { win_config = { position = "bottom", height = 16 } } },
  },
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
    -- Never-pressed git pickers, removed on the keylog's evidence (0 uses in 15
    -- days). They come from LazyVim's snacks_picker extra as plugin-spec keys, so
    -- a `false` entry here unmaps them without forking the extra. The two other
    -- dead defaults, <leader>gG and <leader>gY, are not spec keys and are deleted
    -- in lua/config/keymaps.lua instead.
    keys = {
      { "<leader>gS", false }, -- Git Stash
      { "<leader>gi", false }, -- GitHub Issues (open)
      { "<leader>gI", false }, -- GitHub Issues (all)
      { "<leader>gp", false }, -- GitHub Pull Requests (open)
      { "<leader>gP", false }, -- GitHub Pull Requests (all)
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
