return function(stops_path)
  local tour_stops = dofile(stops_path)
  local namespace = vim.api.nvim_create_namespace("tx_code_tour")
  vim.diagnostic.reset(namespace)

  local diagnostics_by_buffer = {}
  local quickfix_items = {}

  for _, stop in ipairs(tour_stops) do
    local buffer_number = vim.fn.bufadd(stop.file)
    vim.fn.bufload(buffer_number)
    diagnostics_by_buffer[buffer_number] = diagnostics_by_buffer[buffer_number] or {}
    table.insert(diagnostics_by_buffer[buffer_number], {
      lnum = stop.line - 1,
      col = 0,
      severity = vim.diagnostic.severity[stop.severity],
      message = stop.message,
      source = "tour",
      user_data = { tour_label = stop.label },
    })
    table.insert(quickfix_items, {
      filename = stop.file,
      lnum = stop.line,
      col = 1,
      text = stop.label .. " — " .. vim.split(stop.message, "\n")[1],
    })
  end

  for buffer_number, diagnostics in pairs(diagnostics_by_buffer) do
    vim.diagnostic.set(namespace, buffer_number, diagnostics)
  end

  vim.diagnostic.config({
    virtual_text = {
      prefix = "▶",
      spacing = 2,
      format = function(diagnostic)
        return diagnostic.user_data.tour_label
      end,
    },
    signs = true,
    underline = false,
    float = {
      border = "rounded",
      source = false,
      max_width = 80,
      max_height = 30,
      wrap = true,
    },
  }, namespace)

  local tour_state = {
    index = 1,
    namespace = namespace,
    stops = tour_stops,
  }
  _G.__tx_code_tour = tour_state

  local function set_quickfix()
    vim.fn.setqflist({}, " ", {
      title = "Code tour",
      items = quickfix_items,
    })
  end

  local function open_stop(index)
    if index < 1 or index > #tour_state.stops then
      vim.notify("No more tour stops", vim.log.levels.INFO)
      return
    end

    tour_state.index = index
    local stop = tour_state.stops[index]
    vim.cmd("noswapfile edit " .. vim.fn.fnameescape(stop.file))
    vim.api.nvim_win_set_cursor(0, { stop.line, 0 })
    vim.cmd("normal! zz")
  end

  local function current_index()
    local current_file = vim.fn.fnamemodify(vim.api.nvim_buf_get_name(0), ":p")
    local current_line = vim.api.nvim_win_get_cursor(0)[1]
    for index, stop in ipairs(tour_state.stops) do
      if stop.file == current_file and stop.line == current_line then
        return index
      end
    end
    return tour_state.index
  end

  set_quickfix()

  vim.keymap.set("n", "]n", function()
    open_stop(current_index() + vim.v.count1)
  end, { desc = "Next tour stop" })
  vim.keymap.set("n", "[n", function()
    open_stop(current_index() - vim.v.count1)
  end, { desc = "Previous tour stop" })
  vim.keymap.set("n", "<leader>cn", function()
    vim.diagnostic.open_float(nil, {
      namespace = namespace,
      scope = "line",
      border = "rounded",
      source = false,
    })
  end, { desc = "Show tour note" })
  vim.keymap.set("n", "<leader>cN", function()
    open_stop(1)
  end, { desc = "Restart code tour" })
  vim.keymap.set("n", "<leader>cl", function()
    set_quickfix()
    vim.cmd("copen")
    vim.wo.wrap = true
  end, { desc = "List tour stops" })

  vim.api.nvim_create_user_command("TourClear", function()
    vim.diagnostic.reset(namespace)
    vim.fn.setqflist({}, "f")
    for _, mapping in ipairs({ "]n", "[n", "<leader>cn", "<leader>cN", "<leader>cl" }) do
      vim.keymap.del("n", mapping)
    end
    _G.__tx_code_tour = nil
  end, { force = true })

  open_stop(1)
  return "tour applied"
end
