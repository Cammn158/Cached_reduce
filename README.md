# Cache & Latency HUD (`kv_cache_hud`)

Công cụ chạy cục bộ theo dõi thời gian thực **KV Cache**, tiến độ **chu trình 1 Step** (`Tool Local` $\to$ `Gửi & First-Byte ACK / TTFB` $\to$ `TPU Thinking Stream` $\to$ `Đầu ra & Chốt sổ`), số token `Fresh Input` / `Cache Read` / `Output` chuẩn xác từ `transcript.jsonl`, bộ đếm **Token Tiết Kiệm Khi Mở Tool**, kèm cơ chế bảo vệ quota và kiểm soát đường truyền TCP.

Xem giải thích chi tiết từng nút bấm, chỉ số, màu sắc biểu đồ và 4 Tab giao diện tại [HUONG_DAN_CHUC_NANG_WEB.md](HUONG_DAN_CHUC_NANG_WEB.md).

## Cách Khởi Chạy

> [!info]+ Hai chế độ khởi chạy theo quyền hạn Windows
> - **Chế độ Thường (`run_hud.bat`):** Nháy đúp để mở ngay tại `http://127.0.0.1:3380` (không hỏi UAC). Hiển thị 100% số token chuẩn, biểu đồ 40 bước, cây vàng, TTL 5 phút, danh sách socket Google `:443`, và tiến trình `Tool Local`.
> - **Chế độ Toàn phần Admin (`run_hud_admin.bat`):** Yêu cầu quyền Administrator 1 lần khi mở để kích hoạt `SetPerTcpConnectionEStats` (`FULL_KERNEL_ESTATS`), đọc trực tiếp từng byte vào/ra, `ThruBytesAcked`, `SmoothedRtt (ms)` và `PktsRetrans` ở tầng nhân Windows, đồng thời cho phép bật/tắt `TCP Keep-Alive 15s` ngay trên giao diện Web.

Chạy từ dòng lệnh (tùy chọn không tự mở trình duyệt hoặc ép mở cổng mới):

```powershell
cd d:\01_Workspace\Personal_Dev\esp_nghich\kv_cache_hud
python -m kv_cache_hud --no-browser
```

Tạo Shortcut ngoài màn hình Desktop (mặc định trỏ vào `run_hud_admin.bat` và tự mở lại tab trình duyệt nếu cổng `3380` đã chạy sẵn):

```powershell
.\create_shortcut.bat
```

## Kiểm Thử Tự Động

```powershell
cd d:\01_Workspace\Personal_Dev\esp_nghich\kv_cache_hud
python -m unittest discover -s tests -v
```
