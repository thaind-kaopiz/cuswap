# cuswap

Công cụ CLI quản lý nhiều tài khoản **Cursor IDE** trên máy local — giống [claude-swap](https://github.com/realiti4/claude-swap) (`cswap`), nhưng dành cho Cursor.

- Xem danh sách tài khoản, plan, usage, trạng thái
- Đổi account nhanh bằng cách swap session đã lưu
- **Không** cần login/logout trên website mỗi lần đổi
- Chat, settings, extensions của Cursor giữ nguyên (chỉ đổi phần đăng nhập)

> Chỉ dùng với tài khoản **bạn sở hữu**. Token được lưu trên máy bạn (Windows: mã hóa DPAPI). Tool **không** reset machine ID và **không** bypass thanh toán.

## Cài đặt

Cần Python 3.11+ và [uv](https://docs.astral.sh/uv/).

```bash
cd cuswap
uv sync
uv run cuswap --help
```

Cài ra PATH:

```bash
uv tool install -e .
cuswap list
```

## Quy trình dùng

### 1. Lưu account đang login trong Cursor

```bash
cuswap add
```

### 2. Thêm account thứ hai (không logout website)

**Đừng** Sign Out trên cursor.com — việc đó revoke session account cũ.

```bash
cuswap login
```

Cửa sổ ẩn danh sẽ mở. Đăng nhập account mới **ở đó**. Trình duyệt thường và Cursor IDE giữ nguyên account hiện tại.

Sau đó đổi khi cần:

```bash
cuswap switch 2
```

`cuswap switch` phải tắt Cursor rồi mở lại. Nếu chạy lệnh **trong terminal của Cursor**, tool tách process phụ để ghi session sau khi IDE đóng — nếu không, lệnh bị giết giữa chừng và account không đổi.

Lần đầu mỗi account chỉ cần login **một lần** trong cửa sổ ẩn danh. Sau đó `cuswap switch` không đụng website.

### 3. Xem usage / status

```bash
cuswap                # dashboard (giống cswap list)
cuswap list
cuswap status
cuswap watch          # refresh định kỳ
```

### 4. Đổi account

```bash
cuswap switch                 # xoay account tiếp theo
cuswap switch 2               # theo slot
cuswap switch you@email.com
cuswap switch --strategy best # account còn nhiều quota nhất
```

Mặc định tool **tắt rồi mở lại Cursor** để IDE nhận session mới. Bỏ restart (không khuyến nghị):

```bash
cuswap switch 2 --no-restart
```

## Lệnh

| Lệnh | Ý nghĩa |
|------|---------|
| `cuswap` / `cuswap list` | Bảng account + usage + status |
| `cuswap status` | Account đang active |
| `cuswap add` | Lưu session đang login |
| `cuswap switch [target]` | Đổi account |
| `cuswap login` | Thêm account qua cửa sổ ẩn danh |
| `cuswap disable 2` / `enable 2` | Loại khỏi vòng xoay |
| `cuswap remove 2` | Xóa khỏi cuswap (không xóa tài khoản Cursor) |
| `cuswap watch` | Dashboard live |
| `cuswap auto` | Tự đổi khi usage chạm ngưỡng (sẽ restart Cursor) |
| `cuswap tui` | Menu tương tác |
| `cuswap doctor` | Kiểm tra path / session |
| `cuswap doctor --repair` | Sửa `state.vscdb` bị corrupt (phải tắt Cursor) |
| `cuswap export backup.json` | Export (chứa token — giữ kín) |
| `cuswap purge` | Xóa toàn bộ data cuswap |

`list`, `status`, `switch` hỗ trợ `--json` cho script.

## Cách hoạt động

`cuswap` **không** copy cả file `state.vscdb` (cách đó làm Cursor báo *file storage corrupted*). Tool mở DB tại chỗ, chỉ UPDATE key đăng nhập, rồi checkpoint WAL — cùng kiểu ghi atomic (`os.replace` + retry) như [claude-swap](https://github.com/realiti4/claude-swap).

Nếu Cursor đã báo storage corrupted, tắt hết Cursor rồi chạy `cuswap doctor --repair`.

Cursor lưu session trong SQLite:

- Windows: `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux: `~/.config/Cursor/User/globalStorage/state.vscdb`

`cuswap` chỉ đọc/ghi các key đăng nhập (`cursorAuth/*` và vài key identity). Chat history và settings không bị đổi.

Usage lấy từ endpoint dashboard của Cursor (`/api/usage-summary`) bằng session token local — cùng nguồn mà trang Usage trên cursor.com dùng. API này không chính thức, có thể đổi.

`cuswap list` chỉ refresh token khi access token hết hạn hoặc usage API trả 401. Cursor mã hóa `accessToken` / `refreshToken` trong `state.vscdb` (`cursor-safe-storage`); tool giải mã local rồi mới gọi usage. `needs_reauth` chỉ khi session thật sự bị revoke (Sign Out trên website/Cursor) — không phải vì vừa `switch`. Khi đó tool giữ usage lần lấy thành công gần nhất; login lại account đó rồi `cuswap add`.

Data của tool:

| OS | Thư mục |
|----|---------|
| Windows | `%APPDATA%\cuswap\` |
| macOS | `~/Library/Application Support/cuswap/` |
| Linux | `~/.local/share/cuswap/` |

Ghi đè bằng biến môi trường `CUSWAP_HOME`.

## Lưu ý

- Đổi account **cần tắt Cursor** — IDE giữ session trong RAM và có thể ghi đè DB nếu còn mở.
- Token hết hạn: login lại account đó trong Cursor, rồi `cuswap add` để cập nhật.
- `cuswap export` chứa session token. Đừng commit / gửi file này.

## Dev

```bash
uv sync --extra dev
uv run pytest
uv run cuswap doctor
```
