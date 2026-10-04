%global sname pg_local_cache
%global pginstdir /usr/pgsql-%{pgmajorversion}

Name:           %{sname}_%{pgmajorversion}
Version:        3.1.0
Release:        1%{?dist}
Summary:        Transaction-aware PostgreSQL primary-key row cache
License:        MIT
URL:            https://github.com/profundium/pg_local_cache
Source0:        %{sname}-%{version}.tar.gz
BuildRequires:  postgresql%{pgmajorversion}-devel
BuildRequires:  openssl-devel
BuildRequires:  clang
BuildRequires:  llvm
Requires:       postgresql%{pgmajorversion}-server

%description
pg_local_cache is a PostgreSQL extension that caches complete rows by primary
key, with transaction-aware invalidation and an optional authenticated RESP2
endpoint. PostgreSQL remains the source of truth.

%package -n %{sname}_%{pgmajorversion}-llvmjit
Summary:        LLVM bitcode for %{sname}
Requires:       %{sname}_%{pgmajorversion} = %{version}-%{release}

%description -n %{sname}_%{pgmajorversion}-llvmjit
LLVM bitcode for just-in-time compilation of %{sname} functions.

%prep
%setup -q -n %{sname}-%{version}

%build
make PG_CONFIG=%{pginstdir}/bin/pg_config

%install
rm -rf %{buildroot}
make PG_CONFIG=%{pginstdir}/bin/pg_config DESTDIR=%{buildroot} install

%files
%license LICENSE
%doc README.md CHANGELOG.md SECURITY.md
%{pginstdir}/lib/pg_local_cache.so
%{pginstdir}/share/extension/pg_local_cache.control
%{pginstdir}/share/extension/pg_local_cache--*.sql

%files -n %{sname}_%{pgmajorversion}-llvmjit
%{pginstdir}/lib/bitcode/pg_local_cache/
%{pginstdir}/lib/bitcode/pg_local_cache.index.bc
