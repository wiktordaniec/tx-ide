return {
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
}
