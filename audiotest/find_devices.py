import av.option

# Use PyAV's internal library to force a device scan dump to your console
try:
    av.open(file="audio=dummy", format="dshow", options={"list_devices": "true"})
except Exception:
    # This block is expected to fail with an I/O error, 
    # but it will print the device list to your terminal right before it crashes!
    pass
