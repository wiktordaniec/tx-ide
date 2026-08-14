return {
  -- LazyVim's always_show_bufferline=false hides the whole bufferline -- tab
  -- indicators included -- when fewer than two listed buffers exist. Diffview
  -- buffers are unlisted, so diff-heavy sessions lose all sign of their tabs.
  {
    "akinsho/bufferline.nvim",
    opts = { options = { always_show_bufferline = true } },
  },
  {
    "nvim-telescope/telescope.nvim",
    opts = {
      defaults = {
        layout_strategy = "flex",
        layout_config = {
          width = 0.95,
          height = 0.95,
          preview_width = 0.6,
          flex = { flip_columns = 120 },
        },
      },
    },
  },
  {
    "folke/snacks.nvim",
    opts = function(_, opts)
      -- Picker rows are wide -- path + line + code for LSP and grep results, hash +
      -- date + author + subject for git log -- so run the pickers at full window
      -- width. Height is left as the preset has it. In snacks a size of 0 means
      -- "full", anything below 1 is a fraction.
      --
      -- These two presets back every picker that doesn't ask for something special
      -- (snacks picks between them on window width), so widening them covers the lot.
      -- Purpose-built presets are deliberately left alone: the explorer's sidebar and
      -- vim.ui.select's dropdown want to stay small, and ivy is already width 0.
      --
      -- In the horizontal "default" preset the preview is the only child with a set
      -- width and the list flexes into what is left, so 0.6 on the preview splits
      -- them 40/60. Its position in the preset is looked up rather than hardcoded.
      local default = { width = 0 }
      for index, child in ipairs(require("snacks.picker.config.layouts").default.layout) do
        if child.win == "preview" then
          default[index] = { width = 0.6 }
        end
      end

      opts.picker = vim.tbl_deep_extend("force", opts.picker or {}, {
        layouts = {
          default = { layout = default },
          vertical = { layout = { width = 0 } },
        },
      })
    end,
  },
}
