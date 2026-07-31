-- Statusline indicator for the <leader>gF inline diff: shows the picked base
-- commit while b:inline_diff_base is set, so an active inline view is always
-- visible at a glance.
return {
  "nvim-lualine/lualine.nvim",
  opts = function(_, opts)
    table.insert(opts.sections.lualine_c, {
      function()
        local base = vim.b.inline_diff_base
        return base and (" vs " .. base) or ""
      end,
      color = { fg = "#ff966c", gui = "bold" },
    })
    return opts
  end,
}
