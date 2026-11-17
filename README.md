# Bộ bàn giao HTHT — 11/11/2026

Gói gồm bản chụp source server Python và dữ liệu đang lưu trên SS1, các file khác biệt của SS2, tài nguyên CDN có trong manifest, website trước khi đóng, APK Android đã build và bộ công cụ build/mod APK. Bản lấy từ SS3 chưa có trong gói vì VPS không kết nối được qua SSH lúc đóng gói.

## Khởi tạo server độc lập

1. Dùng Ubuntu và Python 3.12. Giải nén vào `/opt/game`.
2. Chạy `python3 run_server.py --realm 1 --initialize-only` và đặt mật khẩu quản trị mới.
3. Chạy `python3 run_server.py --realm 1 --origin http://TEN_MIEN_MOI`.
4. Đặt nginx theo `deploy/nginx.conf.example`, thay tên miền và đường dẫn. Website cần cấu hình PHP-FPM có cURL để xử lý `proxy.php`. Truyền biến `HTHT_BACKENDS` cho PHP-FPM nếu website và game đặt khác máy.
5. Kiểm tra `/sdk/v1/init`, `/pack5` và `/cdn/lua_115.unity3d`. Sửa endpoint trong client rồi thử đăng ký, đăng nhập và vào game.
6. Tạo SS2/SS3 bằng `--realm 2` và `--realm 3`. Mỗi realm có database và cấu hình riêng trong `state/ssN`. Bộ mẫu tạo khóa riêng từng realm. Tài khoản liên server cần cấu hình danh tính dùng chung phù hợp.

`--local-assets` dùng để thử tải tài nguyên bằng Python khi chưa có nginx. Khi triển khai thật, dùng nginx cho `/cdn/`. Có thể đổi API và cổng game bằng `--api-port`, `--game-port`.

Admin ở `server/admin_server.py`, bind loopback. Để chạy, đặt `HTHT_DB`, `HTHT_ACCOUNT_CONFIG`, `HTHT_ADMIN_PORT` rồi chạy file này. Truy cập qua SSH tunnel và dùng tài khoản `owner_admin` cùng mật khẩu vừa khởi tạo. Các thao tác điều khiển systemd/IP trong admin cần cấu hình theo hạ tầng mới trước khi dùng.

## Client Android và bộ công cụ build

`client/HTHT-2.0.0-meta.apk` là APK mẫu đã thay endpoint cũ bằng địa chỉ tài liệu không hoạt động và ký lại bằng khóa thử riêng cho gói bàn giao. Bên nhận cần thay bằng địa chỉ của mình rồi ký bằng khóa riêng. Hai APK trong gói được kiểm tra chữ ký sau khi thay endpoint. Giao diện APK chưa được chạy lại sau bước thay endpoint.

Gói này gồm server Python, tài nguyên client đã đóng gói và công cụ chỉnh sửa APK. Project Unity gốc gồm Assets/ProjectSettings/Packages chưa được tìm thấy trong nguồn bàn giao. Thư mục `decompiled/csharp` và `decompiled/proto` phục vụ tra cứu cấu trúc client.

`ops_new_vps/beta200/build_apk.py` dùng APK nền `ops_new_vps/freedom_brand/Hai-Tac-Tu-Do.apk`, Lua/bundle và hai video trong beta200. Cài `client/requirements-build.txt`, Java và tạo keystore riêng. Đặt `HTHT_KEYSTORE`, `HTHT_KEY_ALIAS`, `HTHT_KEYSTORE_PASSWORD`, tùy chọn `HTHT_KEY_PASSWORD`, rồi chạy `python ops_new_vps/beta200/build_apk.py`. Kiểm tra file APK đầu ra đã ký trước khi cài đặt. Bộ build chưa được chạy lại với khóa của bên nhận.

## Dữ liệu và phạm vi kiểm tra

Database người chơi, mật khẩu VPS/hosting, khóa ký phiên, bản sao lưu tài khoản, log và lịch sử giftcode đã được tách ra. Database mới được tạo tự động bởi source. `database/schema.sql` là cấu trúc tham khảo từ lần khởi tạo sạch.

Runtime đã được sửa để lấy đường dẫn và CDN từ cấu hình hạ tầng mới. Các quy tắc gameplay và tài khoản đặc biệt dành cho admin trong source cần được bên nhận xem lại. File khác biệt SS2 nằm trong `realm-overrides/SS2`, để đối chiếu khi cần.

Trước lúc đóng game, SS1/SS2 có tình trạng nghẽn hàng chờ, lỗi SQLite locked và nhiều tác vụ 913 kéo dài. Việc đóng gói chưa xử lý lỗi tải cao đó. Kết quả khởi tạo và smoke test được ghi trong `KIEM_TRA_BAN_GIAO.json`. Cần kiểm thử tải và kiểm tra giao diện APK trên endpoint mới trước khi mở cho người chơi.

`FILE_SHA256.json` liệt kê dấu kiểm tra các file để đối chiếu sau khi giải nén.
